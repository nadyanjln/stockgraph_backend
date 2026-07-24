"""User repository — all DB access for the User entity."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self, username: str, password: str, email: str, name: str
    ) -> User:
        user = User(username=username, name=name, password=password, email=email)
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.strip().lower())
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_supabase_user_id(self, user_id: str) -> User | None:
        stmt = select(User).where(User.supabase_user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists(self, user_id: int) -> bool:
        stmt = select(User.id).where(User.id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None
