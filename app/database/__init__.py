"""Database package.

Note: `Base` is safe to import without DB driver present.
Engine/session imports live in `app.database.session` and must be
imported explicitly (they require `asyncpg`).
"""

from app.database.base import Base

__all__ = ["Base"]
