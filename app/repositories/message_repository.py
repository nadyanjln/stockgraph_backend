"""Message repository."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message, SenderEnum


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        conversation_id: int,
        sender: SenderEnum,
        message: str,
        citations: list[str] | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> Message:
        row = Message(
            conversation_id=conversation_id,
            sender=sender,
            message=message,
            citations=list(citations or []),
            sources=list(sources or []),
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def list_by_conversation(
        self,
        conversation_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
