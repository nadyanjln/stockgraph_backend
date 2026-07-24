"""Conversation business logic."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.user_repository import UserRepository
from app.utils.exceptions import NotFoundError


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ConversationRepository(session)
        self.user_repo = UserRepository(session)

    async def create(self, user_id: int, title: str | None) -> Conversation:
        if not await self.user_repo.exists(user_id):
            raise NotFoundError(f"user {user_id} not found")
        convo = await self.repo.create(user_id=user_id, title=title)
        await self.session.commit()
        return convo

    async def list_for_user(
        self, user_id: int, *, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        if not await self.user_repo.exists(user_id):
            raise NotFoundError(f"user {user_id} not found")
        return await self.repo.list_by_user(user_id, limit=limit, offset=offset)

    async def get(self, conversation_id: int) -> Conversation:
        convo = await self.repo.get_by_id(conversation_id)
        if convo is None:
            raise NotFoundError(f"conversation {conversation_id} not found")
        return convo
