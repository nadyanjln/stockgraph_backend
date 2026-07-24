"""Message business logic.

Sending a message in one call:
  1. Persists the user message.
  2. Generates a bot reply (pluggable).
  3. Persists the bot message.
  4. Commits once, atomically.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message, SenderEnum
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository
from app.utils.bot import generate_reply
from app.utils.exceptions import NotFoundError


class MessageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = MessageRepository(session)
        self.convo_repo = ConversationRepository(session)

    async def send(
        self, conversation_id: int, text: str, user_id: int
    ) -> tuple[Message, Message]:
        convo = await self.convo_repo.get_by_id(conversation_id)
        if convo is None or convo.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id} not found")

        user_msg = await self.repo.create(
            conversation_id=conversation_id,
            sender=SenderEnum.USER,
            message=text,
        )
        reply_text = await generate_reply(text)
        bot_msg = await self.repo.create(
            conversation_id=conversation_id,
            sender=SenderEnum.BOT,
            message=reply_text,
        )
        await self.session.commit()
        return user_msg, bot_msg

    async def log_pair(
        self,
        conversation_id: int,
        user_text: str,
        bot_text: str,
        user_id: int,
        citations: list[str] | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> tuple[Message, Message]:
        """Persist a user message and an externally-produced bot reply as-is."""
        convo = await self.convo_repo.get_by_id(conversation_id)
        if convo is None or convo.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id} not found")

        user_msg = await self.repo.create(
            conversation_id=conversation_id,
            sender=SenderEnum.USER,
            message=user_text,
        )
        bot_msg = await self.repo.create(
            conversation_id=conversation_id,
            sender=SenderEnum.BOT,
            message=bot_text,
            citations=citations,
            sources=sources,
        )
        await self.session.commit()
        return user_msg, bot_msg

    async def list_in_conversation(
        self,
        conversation_id: int,
        *,
        user_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        convo = await self.convo_repo.get_by_id(conversation_id)
        if convo is None or convo.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id} not found")
        return await self.repo.list_by_conversation(
            conversation_id, limit=limit, offset=offset
        )
