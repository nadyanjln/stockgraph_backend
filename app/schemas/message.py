"""Message schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.message import SenderEnum


class MessageCreate(BaseModel):
    message: str = Field(..., min_length=1, description="Message body from the user")


class MessageLog(BaseModel):
    """Persist a user turn together with an externally-produced bot reply.

    Used when the bot answer comes from the GraphRAG engine (via WebSocket)
    rather than the built-in stub reply generator.
    """

    user_message: str = Field(..., min_length=1)
    bot_message: str = Field(..., min_length=1)
    citations: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender: SenderEnum
    message: str
    citations: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class SendMessageResponse(BaseModel):
    user_message: MessageOut
    bot_message: MessageOut
