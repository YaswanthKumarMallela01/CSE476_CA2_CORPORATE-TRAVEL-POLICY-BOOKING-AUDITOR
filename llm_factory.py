"""
LLM factory: returns a Semantic Kernel Google Gemini chat completion service
and provides an async helper for structured LLM calls with rate limiting and caching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.google.google_ai import GoogleAIChatCompletion
from semantic_kernel.contents import ChatHistory

import config
from core.rate_limiter import DiskCache, RateLimiter, call_with_backoff

logger = logging.getLogger(__name__)

# Shared singletons
_rate_limiter: Optional[RateLimiter] = None
_disk_cache: Optional[DiskCache] = None


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(rpm=config.RATE_LIMIT_RPM)
    return _rate_limiter


def get_disk_cache() -> DiskCache:
    global _disk_cache
    if _disk_cache is None:
        _disk_cache = DiskCache(cache_dir=config.LLM_CACHE_DIR)
    return _disk_cache


def create_kernel() -> Kernel:
    """Create a Semantic Kernel instance with Gemini chat completion service."""
    api_key = config.require_api_key()
    kernel = Kernel()
    service = GoogleAIChatCompletion(
        gemini_model_id=config.GEMINI_MODEL_NAME,
        api_key=api_key,
    )
    kernel.add_service(service)
    return kernel


def get_chat_service() -> GoogleAIChatCompletion:
    """Create a standalone Gemini chat completion service."""
    api_key = config.require_api_key()
    return GoogleAIChatCompletion(
        gemini_model_id=config.GEMINI_MODEL_NAME,
        api_key=api_key,
    )


async def llm_call(
    prompt: str,
    system_message: str = "",
    schema_hint: str = "",
    use_cache: bool = True,
) -> str:
    """Make a rate-limited, cached LLM call.
    
    Args:
        prompt: The user prompt.
        system_message: Optional system message.
        schema_hint: Schema description for cache key differentiation.
        use_cache: Whether to use the disk cache.
    
    Returns:
        The LLM response text.
    
    Raises:
        Exception: If all retries exhausted and no cached fallback.
    """
    cache = get_disk_cache()
    model = config.GEMINI_MODEL_NAME

    # Check cache first
    if use_cache:
        cached = cache.get(model, prompt, schema_hint)
        if cached is not None:
            logger.debug("Using cached LLM response")
            return cached

    # Make the actual call
    service = get_chat_service()
    history = ChatHistory()
    if system_message:
        history.add_system_message(system_message)
    history.add_user_message(prompt)

    limiter = get_rate_limiter()

    async def _do_call():
        result = await service.get_chat_message_contents(history)
        return str(result[0]) if result else ""

    try:
        response = await call_with_backoff(
            _do_call,
            max_retries=config.RATE_LIMIT_MAX_RETRIES,
            rate_limiter=limiter,
        )
    except Exception as e:
        logger.error("LLM call failed after retries: %s", e)
        raise

    # Cache the response
    if use_cache and response:
        cache.put(model, prompt, response, schema_hint)

    return response


async def llm_json_call(
    prompt: str,
    system_message: str = "",
    schema_hint: str = "",
) -> dict[str, Any]:
    """Make an LLM call and parse the response as JSON.
    
    Handles markdown code fences and common JSON issues.
    
    Returns:
        Parsed JSON dict, or empty dict on parse failure.
    """
    raw = await llm_call(prompt, system_message, schema_hint)

    # Strip markdown code fences
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse LLM JSON response: %s", e)
        logger.debug("Raw response: %s", text[:500])
        return {}
