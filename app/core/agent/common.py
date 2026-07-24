"""Shared types and OpenAI helpers for StockGraph agents."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from dotenv import load_dotenv

from app.core.openai_client import async_chat_completion, async_stream_chat_completion

load_dotenv()

NEWS_MODEL = os.getenv("NEWS_MODEL", "gpt-4o-mini")
FINANCIAL_MODEL = os.getenv("FINANCIAL_MODEL", "gpt-4o-mini")
MANAGER_MODEL = os.getenv("MANAGER_MODEL", "gpt-4.1")

AgentName = Literal["news", "financial", "manager"]


def specialist_llm_enabled() -> bool:
    """Whether news/financial agents should summarize context with their own LLM call."""
    return os.getenv("STOCKGRAPH_SPECIALIST_LLM_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class AgentContext:
    """Context snippets and citations collected before an agent LLM call."""

    snippets: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    graph_paths: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


async def chat_complete(
    messages: list[ChatMessage],
    model: str,
    temperature: float = 0.2,
    response_format: dict | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    purpose: str = "Chat Completion",
    caller: str = "app.core.agent.common.chat_complete",
) -> str:
    """Run a non-streaming chat completion without LangChain."""
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if top_p is not None:
        kwargs["top_p"] = top_p
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    response = await async_chat_completion(
        caller=caller,
        purpose=purpose,
        **kwargs,
    )
    return (response.choices[0].message.content or "").strip()


async def stream_chat(
    messages: list[ChatMessage],
    model: str,
    temperature: float = 0.2,
    top_p: float | None = None,
    max_tokens: int | None = None,
    purpose: str = "Streaming Chat Completion",
    caller: str = "app.core.agent.common.stream_chat",
) -> AsyncIterator[str]:
    """Stream chat tokens directly from the OpenAI async SDK."""
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if top_p is not None:
        kwargs["top_p"] = top_p
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    async for delta in async_stream_chat_completion(
        caller=caller,
        purpose=purpose,
        **kwargs,
    ):
        yield delta


def trim_history(messages: list[ChatMessage], max_turns: int = 3) -> list[ChatMessage]:
    """Keep the latest user-assistant pairs."""
    pairs: list[tuple[ChatMessage, ChatMessage]] = []
    pending_user: ChatMessage | None = None
    for msg in messages:
        if msg["role"] == "user":
            pending_user = msg
        elif msg["role"] == "assistant" and pending_user is not None:
            pairs.append((pending_user, msg))
            pending_user = None

    flat: list[ChatMessage] = []
    for user, assistant in pairs[-max_turns:]:
        flat.extend([user, assistant])
    return flat


__all__ = [
    "AgentContext",
    "AgentName",
    "ChatMessage",
    "NEWS_MODEL",
    "FINANCIAL_MODEL",
    "MANAGER_MODEL",
    "chat_complete",
    "specialist_llm_enabled",
    "stream_chat",
    "trim_history",
]
