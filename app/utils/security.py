"""Password hashing helpers for legacy/local database compatibility."""

from __future__ import annotations

import bcrypt

_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    """Hash the inaccessible placeholder stored for Supabase-owned accounts."""
    raw = plain.encode("utf-8")[:_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def is_hashed(stored: str) -> bool:
    """Identify legacy plaintext rows during the existing startup migration."""
    return stored.startswith(("$2a$", "$2b$", "$2y$"))
