"""Async SQLAlchemy engine, session factory, and FastAPI dependency."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from app.database.base import Base
from app.database.config import get_db_settings
from app.utils.security import hash_password, is_hashed

_settings = get_db_settings()

async_engine = create_async_engine(
    _settings.async_dsn,
    echo=_settings.echo,
    pool_size=_settings.pool_size,
    max_overflow=_settings.max_overflow,
    pool_pre_ping=True,
    future=True,
)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a transactional session."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables. Idempotent — safe to call on startup."""
    # Import models so they register on Base.metadata before create_all.
    from app import models  # noqa: F401

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(100)"
            ))
            await conn.execute(text(
                "UPDATE users SET name = username WHERE name IS NULL OR name = ''"
            ))
            await conn.execute(text(
                "ALTER TABLE users ALTER COLUMN name SET NOT NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "email_verified_at TIMESTAMPTZ NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "google_id VARCHAR(255) NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "supabase_user_id VARCHAR(64) NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "avatar_url VARCHAR(1000) NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "provider VARCHAR(32) NOT NULL DEFAULT 'email'"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "verification_token_hash VARCHAR(64) NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "verification_token_expires_at TIMESTAMPTZ NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "reset_token_hash VARCHAR(64) NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "reset_token_expires_at TIMESTAMPTZ NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "session_version INTEGER NOT NULL DEFAULT 1"
            ))
            await conn.execute(text(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                "citations JSONB NOT NULL DEFAULT '[]'::jsonb"
            ))
            await conn.execute(text(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                "sources JSONB NOT NULL DEFAULT '[]'::jsonb"
            ))
            await conn.execute(text(
                "UPDATE users SET email = lower(username) || '@legacy.stockgraph.local' "
                "WHERE email IS NULL OR email = ''"
            ))
            await conn.execute(text("UPDATE users SET email = lower(email)"))
            await conn.execute(text(
                "UPDATE users SET email_verified_at = CURRENT_TIMESTAMP "
                "WHERE email LIKE '%@legacy.stockgraph.local' AND email_verified_at IS NULL"
            ))
            await conn.execute(text(
                "ALTER TABLE users ALTER COLUMN email SET NOT NULL"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email_lower "
                "ON users (lower(email)) WHERE email IS NOT NULL"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id "
                "ON users (google_id) WHERE google_id IS NOT NULL"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_supabase_user_id "
                "ON users (supabase_user_id) WHERE supabase_user_id IS NOT NULL"
            ))
            legacy_passwords = await conn.execute(text("SELECT id, password FROM users"))
            for row in legacy_passwords:
                if not is_hashed(row.password):
                    await conn.execute(
                        text("UPDATE users SET password = :password WHERE id = :user_id"),
                        {"password": hash_password(row.password), "user_id": row.id},
                    )


async def dispose_db() -> None:
    """Close the engine and its connection pool. Call on shutdown."""
    await async_engine.dispose()
