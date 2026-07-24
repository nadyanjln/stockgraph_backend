from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from app.dependencies.auth import get_current_user
from app.main import app
from app.services.supabase_auth_service import UnauthorizedError

pytestmark = pytest.mark.black_box


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer ", "Basic abc"])
async def test_bb_be_015_missing_or_empty_bearer_is_rejected(authorization):
    app.dependency_overrides.clear()
    headers = {"Authorization": authorization} if authorization else {}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_bb_be_016_invalid_supabase_token_is_rejected(monkeypatch):
    app.dependency_overrides.clear()

    class RejectingAuth:
        def authenticate(self, token):
            raise UnauthorizedError("invalid session")

    monkeypatch.setattr("app.dependencies.auth.SupabaseAuthService", RejectingAuth)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/users/me", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401
    assert "invalid" in response.json()["message"]
    assert "Traceback" not in response.text
