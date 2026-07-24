"""Centralized OpenAI client helpers for StockGraph.

All direct OpenAI SDK calls in the application should go through this module so
request origin, purpose, and parameters are visible in logs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI

load_dotenv()

logger = logging.getLogger("stockgraph.openai")

_sync_client: OpenAI | None = None
_async_client: AsyncOpenAI | None = None


def openai_client() -> OpenAI:
    """Return a cached sync OpenAI client."""
    global _sync_client
    if _sync_client is None:
        _sync_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _sync_client


def async_openai_client() -> AsyncOpenAI:
    """Return a cached async OpenAI client."""
    global _async_client
    if _async_client is None:
        _async_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _async_client


def _log_request(
    *,
    caller: str,
    purpose: str,
    endpoint: str,
    model: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "file_function": caller,
        "purpose": purpose,
        "endpoint": endpoint,
        "model": model or "",
    }
    if extra:
        payload.update(extra)
    logger.info("openai_request=%s", payload)


def chat_completion(
    *,
    caller: str,
    purpose: str,
    **kwargs: Any,
) -> Any:
    """Run a sync Chat Completions request with standardized logging."""
    messages = kwargs.get("messages") or []
    _log_request(
        caller=caller,
        purpose=purpose,
        endpoint="chat.completions.create",
        model=kwargs.get("model"),
        extra={
            "stream": bool(kwargs.get("stream")),
            "messages": len(messages),
            "response_format": bool(kwargs.get("response_format")),
        },
    )
    return openai_client().chat.completions.create(**kwargs)


async def async_chat_completion(
    *,
    caller: str,
    purpose: str,
    **kwargs: Any,
) -> Any:
    """Run an async Chat Completions request with standardized logging."""
    messages = kwargs.get("messages") or []
    _log_request(
        caller=caller,
        purpose=purpose,
        endpoint="chat.completions.create",
        model=kwargs.get("model"),
        extra={
            "stream": bool(kwargs.get("stream")),
            "messages": len(messages),
            "response_format": bool(kwargs.get("response_format")),
        },
    )
    return await async_openai_client().chat.completions.create(**kwargs)


async def async_stream_chat_completion(
    *,
    caller: str,
    purpose: str,
    **kwargs: Any,
) -> AsyncIterator[str]:
    """Stream Chat Completions text deltas with standardized logging."""
    stream = await async_chat_completion(
        caller=caller,
        purpose=purpose,
        **{**kwargs, "stream": True},
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


def embedding_create(
    *,
    caller: str,
    purpose: str,
    **kwargs: Any,
) -> Any:
    """Run a sync Embeddings request with standardized logging."""
    input_value = kwargs.get("input")
    input_count = len(input_value) if isinstance(input_value, list) else 1
    _log_request(
        caller=caller,
        purpose=purpose,
        endpoint="embeddings.create",
        model=kwargs.get("model"),
        extra={"input_count": input_count},
    )
    return openai_client().embeddings.create(**kwargs)


async def async_embedding_create(
    *,
    caller: str,
    purpose: str,
    **kwargs: Any,
) -> Any:
    """Run an async Embeddings request with standardized logging."""
    input_value = kwargs.get("input")
    input_count = len(input_value) if isinstance(input_value, list) else 1
    _log_request(
        caller=caller,
        purpose=purpose,
        endpoint="embeddings.create",
        model=kwargs.get("model"),
        extra={"input_count": input_count},
    )
    return await async_openai_client().embeddings.create(**kwargs)


__all__ = [
    "async_chat_completion",
    "async_embedding_create",
    "async_openai_client",
    "async_stream_chat_completion",
    "chat_completion",
    "embedding_create",
    "openai_client",
]
