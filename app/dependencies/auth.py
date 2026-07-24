"""FastAPI authentication dependencies backed by Supabase Auth."""

from __future__ import annotations

import asyncio

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.models.user import User
from app.services.supabase_auth_service import SupabaseAuthService, UnauthorizedError
from app.services.user_service import UserService


async def get_current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("authentication required")

    token = authorization.split(" ", 1)[1].strip()
    identity = await asyncio.to_thread(SupabaseAuthService().authenticate, token)
    return await UserService(session).sync_supabase_user(identity)
