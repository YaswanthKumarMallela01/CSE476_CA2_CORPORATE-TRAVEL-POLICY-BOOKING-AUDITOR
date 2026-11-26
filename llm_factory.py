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
from semantic_kernel.connectors.ai.google.google_ai import (
    GoogleAIChatCompletion,
    GoogleAIChatPromptExecutionSettings,
)
from semantic_kernel.contents import ChatHistory

import config
from core.rate_limiter import DiskCache, RateLimiter, call_with_backoff

logger = logging.getLogger(__name__)

# Shared singletons
_rate_limiter: Optional[RateLimiter] = None
_disk_cache: Optional[DiskCache] = None
_resolved_model: Optional[str] = None
_embed_fn = None

FALLBACK_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-flash-latest",
]


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


def get_active_model() -> str:
    """Return the currently active/working Gemini model name."""
    return _resolved_model or config.GEMINI_MODEL_NAME


def get_embeddings_helper():
    """Return local sentence-transformers / ONNX embeddings helper."""
    global _embed_fn
    if _embed_fn is None:
        from core.rag import _get_embed_fn
        _embed_fn = _get_embed_fn(config.EMBEDDING_MODEL_NAME)
    return _embed_fn


def create_kernel(plugins: Optional[dict[str, Any]] = None) -> Kernel:
    """Create a Semantic Kernel instance with Gemini chat completion service and plugins."""
    kernel = Kernel()
    if config.GOOGLE_API_KEY:
        service = GoogleAIChatCompletion(
            gemini_model_id=get_active_model(),
            api_key=config.GOOGLE_API_KEY,
        )
        kernel.add_service(service)
    if plugins:
        for plugin_name, plugin_obj in plugins.items():
            kernel.add_plugin(plugin_obj, plugin_name=plugin_name)
    return kernel


def get_chat_service(model_name: Optional[str] = None) -> GoogleAIChatCompletion:
    """Create a standalone Gemini chat completion service."""
    api_key = config.require_api_key()
    return GoogleAIChatCompletion(
        gemini_model_id=model_name or get_active_model(),
        api_key=api_key,
    )


def _get_execution_settings() -> GoogleAIChatPromptExecutionSettings:
    """Create execution settings for Gemini chat completion."""
    return GoogleAIChatPromptExecutionSettings(
        temperature=0.2,
        max_tokens=1024,
    )


async def llm_call(
    prompt: str,
    system_message: str = "",
    schema_hint: str = "",
    use_cache: bool = True,
) -> str:
    """Make a rate-limited, cached LLM call with automatic model fallback on 404/503."""
    global _resolved_model
    cache = get_disk_cache()
    active_model = get_active_model()

    # Check cache first
    if use_cache:
        cached = cache.get(active_model, prompt, schema_hint)
        if cached is not None:
            logger.debug("Using cached LLM response")
            return cached

    # Build candidate model order
    candidates = [active_model] + [m for m in FALLBACK_MODELS if m != active_model]
    history = ChatHistory()
    if system_message:
        history.add_system_message(system_message)
    history.add_user_message(prompt)
    settings = _get_execution_settings()
    limiter = get_rate_limiter()

    last_exc: Optional[Exception] = None
    for idx, model_candidate in enumerate(candidates):
        service = get_chat_service(model_candidate)

        async def _do_call():
            result = await service.get_chat_message_contents(history, settings)
            return str(result[0]) if result else ""

        try:
            response = await call_with_backoff(
                _do_call,
                max_retries=1 if idx < len(candidates) - 1 else config.RATE_LIMIT_MAX_RETRIES,
                rate_limiter=limiter,
            )
            if model_candidate != active_model:
                logger.info("Gemini model resolved/switched to '%s'", model_candidate)
            _resolved_model = model_candidate
            if use_cache and response:
                cache.put(model_candidate, prompt, response, schema_hint)
            return response
        except Exception as e:
            last_exc = e
            err_msg = str(e).lower()
            if any(code in err_msg for code in ("404", "not_found", "503", "unavailable", "no longer available")):
                logger.warning("Model '%s' unavailable (%s); trying next candidate...", model_candidate, str(e)[:80])
                continue
            logger.error("LLM call failed on '%s': %s", model_candidate, e)
            raise

    logger.error("All Gemini model candidates failed: %s", last_exc)
    if last_exc:
        raise last_exc
    raise RuntimeError("No Gemini model candidates succeeded")


async def llm_json_call(
    prompt: str,
    system_message: str = "",
    schema_hint: str = "",
) -> dict[str, Any]:
    """Make an LLM call and parse the response as JSON."""
    raw = await llm_call(prompt, system_message, schema_hint)

    # Strip markdown code fences
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try extracting first JSON object or array
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        logger.warning("Failed to parse LLM JSON response: %s", text[:200])
        return {}
