from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.routes import conversations, endpoint, merger_routes, messages
from app.services.database.graphrag_engine import QueryResult

pytestmark = pytest.mark.black_box


def conversation_row(conversation_id: int = 11, user_id: int = 7):
    return SimpleNamespace(
        id=conversation_id,
        user_id=user_id,
        title="Analisis BBCA",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def message_row(
    message_id: int,
    sender: str,
    text: str,
    *,
    citations=None,
    sources=None,
):
    return SimpleNamespace(
        id=message_id,
        conversation_id=11,
        sender=sender,
        message=text,
        citations=citations or [],
        sources=sources or [],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_bb_be_001_health_is_safe_when_engine_is_unavailable(api_client):
    for path in ("/health", "/api/health"):
        response = await api_client.get(path)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert "Traceback" not in response.text
        assert "C:\\" not in response.text
        assert "password" not in response.text.lower()


@pytest.mark.asyncio
async def test_bb_be_002_health_reports_available_engine(api_client, test_app):
    test_app.state.engine = SimpleNamespace(
        is_available=True,
        connection_error="",
        available_years=[2024, 2025],
    )
    response = await api_client.get("/health")
    assert response.status_code == 200
    assert response.json()["engine_ready"] is True
    assert response.json()["years"] == [2024, 2025]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({}, "stock_codes"),
        ({"stock_codes": []}, "stock_codes"),
        ({"stock_codes": ["BBCA"], "max_articles": 0}, "max_articles"),
        ({"stock_codes": ["BBCA"], "max_articles": 21}, "max_articles"),
        ({"stock_codes": ["BBCA"], "threshold": -0.1}, "threshold"),
        ({"stock_codes": ["BBCA"], "threshold": 1.1}, "threshold"),
        ({"stock_codes": "BBCA"}, "stock_codes"),
    ],
)
async def test_bb_be_003_pipeline_rejects_invalid_payloads(api_client, payload, field):
    response = await api_client.post("/api/merger/pipeline", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "validation_error"
    assert field in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stock_codes", [["BBCA"], ["BBCA", "BMRI"], ["bbca"], [" BBCA "]])
async def test_bb_be_004_pipeline_accepts_valid_ticker_shapes(
    api_client, monkeypatch, stock_codes
):
    observed: dict[str, object] = {}

    def fake_fetch(codes, try_idx_pdf=True):
        observed["codes"] = codes
        return {}

    monkeypatch.setattr(merger_routes, "fetch_multiple", fake_fetch)
    monkeypatch.setattr(merger_routes, "extract_search_keywords", lambda *args, **kwargs: [])
    monkeypatch.setattr(merger_routes, "crawl_by_keywords", lambda *args, **kwargs: [])
    monkeypatch.setattr(merger_routes, "extract_all", lambda *args, **kwargs: {})
    monkeypatch.setattr(merger_routes, "filter_results", lambda *args, **kwargs: {})
    monkeypatch.setattr(merger_routes, "build_conversation_insight_snapshot", lambda *args: {})

    async def fake_build(*args, **kwargs):
        return {}

    monkeypatch.setattr(merger_routes, "build_graph_multi_tenant_async", fake_build)
    response = await api_client.post(
        "/api/merger/pipeline",
        json={"stock_codes": stock_codes, "question": "", "max_articles": 4},
    )
    assert response.status_code == 200
    assert observed["codes"] == stock_codes
    assert response.json()["graphs_built"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), TimeoutError("provider timeout")])
async def test_bb_be_005_pipeline_external_failure_is_sanitized(api_client, monkeypatch, failure):
    monkeypatch.setattr(merger_routes, "fetch_multiple", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    response = await api_client.post("/api/merger/pipeline", json={"stock_codes": ["BBCA"]})
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "provider" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_bb_be_006_query_returns_graphrag_answer_and_validated_sources(api_client, test_app):
    class FakeEngine:
        async def query(self, question, year=None, return_context=False):
            return QueryResult(
                question=question,
                answer="Laba meningkat berdasarkan laporan resmi.",
                year=year or 2025,
                citations=["[1]"],
                context="evidence",
                sources=[{
                    "source_id": "financial:BBCA:2025",
                    "source_type": "financial_report",
                    "title": "Laporan BBCA",
                    "url": "",
                    "snippet": "Laba meningkat.",
                }],
            )

    test_app.state.engine = FakeEngine()
    response = await api_client.post("/api/query", json={"question": "Bagaimana laba BBCA?", "year": 2025})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"].startswith("Laba meningkat")
    assert body["sources"][0]["source_id"] == "financial:BBCA:2025"


@pytest.mark.asyncio
async def test_bb_be_007_query_gracefully_reports_unavailable_engine(api_client):
    response = await api_client.post("/api/query", json={"question": "Bagaimana BBCA?"})
    assert response.status_code == 200
    assert response.json() == {"error": "GraphRAG engine is not initialized"}


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 1, 2])
async def test_bb_be_008_graph_explorer_forwards_supported_depth(api_client, monkeypatch, depth):
    monkeypatch.setattr(
        endpoint,
        "explore_provenance_graph",
        lambda **kwargs: {"nodes": [], "edges": [], "depth": kwargs["depth"]},
    )
    response = await api_client.get(f"/api/graph/explore?stock_codes=BBCA&depth={depth}")
    assert response.status_code == 200
    assert response.json()["depth"] == depth


@pytest.mark.asyncio
async def test_bb_be_009_graph_explorer_rejects_excessive_depth(api_client):
    response = await api_client.get("/api/graph/explore?stock_codes=BBCA&depth=4")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.asyncio
async def test_bb_be_010_key_financials_uses_cache(api_client, monkeypatch):
    endpoint._KEY_FIN_CACHE.clear()
    calls = 0

    def fake_extract(code, use_llm):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            stock_code=code,
            company_name="Bank Central Asia",
            source="IDX",
            generated_at="2026-01-01",
            columns=[SimpleNamespace(label="2025", period="FY", year=2025)],
            rows=[SimpleNamespace(label="Revenue", values=[100], formatted=["Rp100"])],
        )

    monkeypatch.setattr(endpoint, "extract_key_financials", fake_extract)
    first = await api_client.get("/api/key-financials/bbca?use_llm=false")
    second = await api_client.get("/api/key-financials/bbca?use_llm=false")
    assert first.status_code == second.status_code == 200
    assert first.json()["stock_code"] == "BBCA"
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("bad numeric data"), TimeoutError("provider timeout")])
async def test_bb_be_011_key_financials_provider_error_is_controlled(api_client, monkeypatch, failure):
    endpoint._KEY_FIN_CACHE.clear()
    monkeypatch.setattr(endpoint, "extract_key_financials", lambda *args: (_ for _ in ()).throw(failure))
    response = await api_client.get("/api/key-financials/BBCA")
    assert response.status_code == 502
    assert response.json()["detail"].startswith("failed to extract key financials")
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_bb_be_012_conversation_and_messages_visible_to_owner(api_client, monkeypatch):
    class FakeConversationService:
        def __init__(self, session): pass
        async def create(self, user_id, title): return conversation_row(user_id=user_id)
        async def list_for_user(self, user_id, **kwargs): return [conversation_row(user_id=user_id)]

    class FakeMessageService:
        def __init__(self, session): pass
        async def list_in_conversation(self, conversation_id, **kwargs):
            return [
                message_row(1, "user", "Pertanyaan"),
                message_row(
                    2,
                    "bot",
                    "Jawaban [1]",
                    citations=["[1]"],
                    sources=[{"source_id": "news-1", "title": "Berita BBCA"}],
                ),
            ]
        async def log_pair(
            self,
            conversation_id,
            user_text,
            bot_text,
            user_id,
            citations=None,
            sources=None,
        ):
            return (
                message_row(1, "user", user_text),
                message_row(
                    2,
                    "bot",
                    bot_text,
                    citations=citations,
                    sources=sources,
                ),
            )

    monkeypatch.setattr(conversations, "ConversationService", FakeConversationService)
    monkeypatch.setattr(messages, "MessageService", FakeMessageService)
    created = await api_client.post("/api/v1/conversations", json={"title": "Analisis BBCA"})
    listed = await api_client.get("/api/v1/conversations/users/7")
    history = await api_client.get("/api/v1/conversations/11/messages")
    logged = await api_client.post(
        "/api/v1/conversations/11/messages/log",
        json={
            "user_message": "Pertanyaan",
            "bot_message": "Jawaban [1]",
            "citations": ["[1]"],
            "sources": [{"source_id": "news-1", "title": "Berita BBCA"}],
        },
    )
    assert created.status_code == logged.status_code == 201
    assert listed.status_code == history.status_code == 200
    assert len(history.json()["data"]) == 2
    assert history.json()["data"][1]["citations"] == ["[1]"]
    assert history.json()["data"][1]["sources"][0]["source_id"] == "news-1"
    assert logged.json()["data"]["bot_message"]["citations"] == ["[1]"]


@pytest.mark.asyncio
async def test_bb_be_013_user_cannot_list_another_users_conversations(api_client):
    response = await api_client.get("/api/v1/conversations/users/8")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/v1/conversations/11/messages", {"message": ""}),
        ("/api/v1/conversations/11/messages/log", {"user_message": "", "bot_message": "ok"}),
    ],
)
async def test_bb_be_014_empty_messages_are_rejected(api_client, path, payload):
    response = await api_client.post(path, json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
