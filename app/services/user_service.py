"""Local profile synchronization for users authenticated by Supabase."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services.supabase_auth_service import SupabaseIdentity
from app.utils.exceptions import ConflictError, NotFoundError
from app.utils.security import hash_password


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)

    async def _unique_username(self, email: str) -> str:
        base = email.split("@", 1)[0][:40] or "user"
        username = base
        suffix = 1
        while await self.repo.get_by_username(username) is not None:
            suffix += 1
            suffix_text = str(suffix)
            username = f"{base[:40 - len(suffix_text)]}{suffix_text}"
        return username

    async def sync_supabase_user(self, identity: SupabaseIdentity) -> User:
        user = await self.repo.get_by_supabase_user_id(identity.user_id)
        already_linked = user is not None
        if user is None:
            user = await self.repo.get_by_email(identity.email)

        if user is not None:
            if user.supabase_user_id and user.supabase_user_id != identity.user_id:
                raise ConflictError(
                    "this email is already linked to another Supabase account"
                )
            updates = {
                "supabase_user_id": identity.user_id,
                "email": identity.email,
                "name": user.name if already_linked and user.name else identity.full_name,
                "avatar_url": identity.avatar_url or user.avatar_url,
                "provider": identity.provider,
                "email_verified_at": user.email_verified_at or datetime.now(timezone.utc),
            }
            changed = any(getattr(user, key) != value for key, value in updates.items())
            if changed:
                for key, value in updates.items():
                    setattr(user, key, value)
                try:
                    await self.session.commit()
                    await self.session.refresh(user)
                except IntegrityError as exc:
                    await self.session.rollback()
                    raise ConflictError(
                        "authenticated account could not be linked"
                    ) from exc
            return user

        try:
            user = await self.repo.create(
                await self._unique_username(identity.email),
                hash_password(secrets.token_urlsafe(32)),
                identity.email,
                identity.full_name,
            )
            user.supabase_user_id = identity.user_id
            user.avatar_url = identity.avatar_url
            user.provider = identity.provider
            user.email_verified_at = datetime.now(timezone.utc)
            await self.session.commit()
            await self.session.refresh(user)
            return user
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("authenticated account could not be linked") from exc

    async def update_display_name(self, user: User, name: str) -> User:
        user.name = " ".join(name.split())
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def get(self, user_id: int) -> User:
        user = await self.repo.get_by_id(user_id)
        if user is None:
            raise NotFoundError(f"user {user_id} not found")
        return user
