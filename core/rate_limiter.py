"""
Shared async rate limiter, exponential backoff with jitter, and disk-based LLM response cache.
All LLM calls in the system go through these utilities.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token-bucket style rate limiter for LLM calls.
    
    Allows at most `rpm` requests per 60-second window.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, rpm: int = 10) -> None:
        self.rpm = rpm
        self.interval = 60.0 / rpm
        self._lock = asyncio.Lock()
        self._last_call: float = 0.0
        self._call_count: int = 0

    async def acquire(self) -> None:
        """Wait until a call slot is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.interval:
                wait_time = self.interval - elapsed
                logger.debug("Rate limiter: waiting %.2fs", wait_time)
                await asyncio.sleep(wait_time)
            self._last_call = time.monotonic()
            self._call_count += 1


class DiskCache:
    """On-disk JSON cache keyed by hash of (model, prompt, schema).
    
    Prevents redundant LLM calls during eval reruns.
    """

    def __init__(self, cache_dir: str = "./llm_cache") -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _key(self, model: str, prompt: str, schema: str = "") -> str:
        raw = f"{model}|{prompt}|{schema}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, model: str, prompt: str, schema: str = "") -> Optional[str]:
        """Return cached response or None."""
        key = self._key(model, prompt, schema)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            self.hits += 1
            logger.debug("Cache HIT: %s", key[:12])
            with open(path) as f:
                data = json.load(f)
            return data.get("response")
        self.misses += 1
        return None

    def put(self, model: str, prompt: str, response: str, schema: str = "") -> None:
        """Store a response."""
        key = self._key(model, prompt, schema)
        path = self.cache_dir / f"{key}.json"
        with open(path, "w") as f:
            json.dump({"model": model, "response": response}, f)
        logger.debug("Cache PUT: %s", key[:12])


async def call_with_backoff(
    func,
    *args,
    max_retries: int = 5,
    rate_limiter: Optional[RateLimiter] = None,
    **kwargs,
) -> Any:
    """Call an async function with rate limiting and exponential backoff on 429/503.
    
    Args:
        func: Async callable to invoke.
        max_retries: Maximum retry attempts.
        rate_limiter: Shared RateLimiter instance.
    
    Returns:
        The result of func(*args, **kwargs).
    
    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        if rate_limiter:
            await rate_limiter.acquire()
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            is_retryable = any(code in err_str for code in ("429", "503", "resource_exhausted", "rate", "quota"))
            if not is_retryable or attempt >= max_retries:
                raise
            wait = min(2 ** attempt + random.uniform(0, 1), 60)
            logger.warning(
                "Retryable error (attempt %d/%d): %s. Waiting %.1fs",
                attempt + 1, max_retries, str(e)[:100], wait,
            )
            await asyncio.sleep(wait)
    raise last_exc  # type: ignore[misc]
