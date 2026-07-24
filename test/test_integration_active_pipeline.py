from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import AsyncMock

import app.main as main_module
from app.core.agent.orchestrator import SessionStore
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.user import User
from app.services.conversation_service import ConversationService
from app.services.message_service import MessageService
from app.services.supabase_auth_service import SupabaseIdentity, UnauthorizedError

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_int_be_001_conversation_service_commits_repository_result():
    session = SimpleNamespace(commit=AsyncMock())
    service = ConversationService(session)
    service.user_repo.exists = AsyncMock(return_value=True)
    service.repo.create = AsyncMock(return_value="conversation")
    result = await service.create(7, "BBCA")
    assert result == "conversation"
    service.repo.create.assert_awaited_once_with(user_id=7, title="BBCA")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_int_be_002_message_service_enforces_repository_ownership():
    from app.utils.exceptions import NotFoundError

    session = SimpleNamespace(commit=AsyncMock())
    service = MessageService(session)
    service.convo_repo.get_by_id = AsyncMock(return_value=SimpleNamespace(user_id=99))
    with pytest.raises(NotFoundError):
        await service.log_pair(11, "q", "a", user_id=7)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_int_be_003_message_service_persists_bot_citation_metadata():
    session = SimpleNamespace(commit=AsyncMock())
    service = MessageService(session)
    service.convo_repo.get_by_id = AsyncMock(return_value=SimpleNamespace(user_id=7))
    service.repo.create = AsyncMock(
        side_effect=[
            SimpleNamespace(sender="user"),
            SimpleNamespace(sender="bot"),
        ],
    )
    sources = [{"source_id": "news-1", "title": "Berita BBCA"}]

    await service.log_pair(
        11,
        "Pertanyaan",
        "Jawaban [1]",
        user_id=7,
        citations=["[1]"],
        sources=sources,
    )

    assert service.repo.create.await_args_list[1].kwargs["citations"] == ["[1]"]
    assert service.repo.create.await_args_list[1].kwargs["sources"] == sources
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_int_be_004_auth_dependency_syncs_valid_identity(monkeypatch):
    identity = SupabaseIdentity("supabase-1", "user@example.test", "User", None, "email")
    user = User(id=7, username="user", name="User", password="x", email=identity.email)

    class FakeAuth:
        def authenticate(self, token):
            assert token == "valid"
            return identity

    class FakeUserService:
        def __init__(self, session): pass
        async def sync_supabase_user(self, received):
            assert received == identity
            return user

    monkeypatch.setattr("app.dependencies.auth.SupabaseAuthService", FakeAuth)
    monkeypatch.setattr("app.dependencies.auth.UserService", FakeUserService)
    result = await get_current_user("Bearer valid", object())
    assert result is user


class FakeAuth:
    def authenticate(self, token):
        if token != "valid":
            raise UnauthorizedError("invalid session")
        return SupabaseIdentity("supabase-1", "user@example.test", "User", None, "email")


class FakeUserService:
    def __init__(self, session): pass
    async def sync_supabase_user(self, identity):
        return User(id=7, username="user", name="User", password="x", email=identity.email)


@asynccontextmanager
async def fake_session_factory():
    yield object()


class StreamingOrchestrator:
    async def run_stream(self, session_id, question):
        yield {"type": "progress", "stage": "answer_generation", "status": "running"}
        yield {"type": "token", "delta": "Jawaban"}
        yield {"type": "final", "answer": "Jawaban", "citations": []}


class FailingOrchestrator:
    async def run_stream(self, session_id, question):
        raise RuntimeError("internal details")
        yield


def configure_websocket(monkeypatch, orchestrator):
    monkeypatch.setattr(main_module, "SupabaseAuthService", FakeAuth)
    monkeypatch.setattr(main_module, "UserService", FakeUserService)
    monkeypatch.setattr(main_module, "async_session_factory", fake_session_factory)
    app.state.orchestrator = orchestrator
    app.state.session_store = SessionStore()


def test_int_be_004_websocket_rejects_invalid_token(monkeypatch):
    configure_websocket(monkeypatch, StreamingOrchestrator())
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/chat?access_token=invalid"):
            pass
    assert exc_info.value.code == 4401


def test_int_be_005_websocket_streams_progress_token_and_final(monkeypatch):
    configure_websocket(monkeypatch, StreamingOrchestrator())
    client = TestClient(app)
    with client.websocket_connect("/ws/chat?access_token=valid") as socket:
        socket.send_json({"session_id": "s1", "question": "Bagaimana BBCA?"})
        events = [socket.receive_json(), socket.receive_json(), socket.receive_json()]
    assert [event["type"] for event in events] == ["progress", "token", "final"]
    assert events[-1]["answer"] == "Jawaban"


def test_int_be_006_websocket_malformed_payload_and_failure_have_terminal_events(monkeypatch):
    configure_websocket(monkeypatch, FailingOrchestrator())
    client = TestClient(app)
    with client.websocket_connect("/ws/chat?access_token=valid") as socket:
        socket.send_text("not-json")
        assert socket.receive_json() == {"type": "error", "message": "invalid json"}
        socket.send_json({"session_id": "s1", "question": "Bagaimana BBCA?"})
        terminal = socket.receive_json()
    assert terminal["type"] == "final"
    assert "internal details" not in terminal["answer"]
