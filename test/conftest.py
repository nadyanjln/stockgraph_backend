from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest

from app.database.session import get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.user import User


@pytest.fixture
def current_user() -> User:
    return User(
        id=7,
        username="tester",
        name="Test User",
        password="not-a-real-password",
        email="tester@example.test",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def test_app(current_user: User):
    async def fake_db() -> AsyncIterator[object]:
        yield object()

    async def fake_user() -> User:
        return current_user

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = fake_user
    app.state.engine = None
    app.state.orchestrator = None
    app.state.session_store = None
    yield app
    app.dependency_overrides.clear()
    app.state.engine = None
    app.state.orchestrator = None
    app.state.session_store = None


@pytest.fixture
async def api_client(test_app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=test_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
