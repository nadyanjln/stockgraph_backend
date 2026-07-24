"""Standalone table creation script.

Usage:
    python -m app.database.migrate
"""

from __future__ import annotations

import asyncio

from app.database.session import dispose_db, init_db
from app.utils.logger import get_logger

logger = get_logger("chatbot.migrate")


async def main() -> None:
    logger.info("creating tables (if not exist)…")
    await init_db()
    await dispose_db()
    logger.info("done.")


if __name__ == "__main__":
    asyncio.run(main())
