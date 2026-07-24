from __future__ import annotations

import pytest

from app.core.agent.common import AgentContext
from app.core.agent.orchestrator import Orchestrator
from app.services.database.evidence_retriever import _traverse
from app.services.database.retrieval_optimizer import QueryPlan

pytestmark = pytest.mark.white_box


def registry(edges: list[dict]) -> dict:
    return {"nodes": {}, "articles": {}, "edges": {str(i): edge for i, edge in enumerate(edges)}}


def test_wb_be_001_traverse_zero_hops_keeps_seed_only():
    nodes, edges = _traverse(registry([{"id": "e1", "source": "A", "target": "B"}]), "A", 0)
    assert nodes == {"A"}
    assert edges == []


def test_wb_be_002_traverse_stops_when_frontier_is_empty():
    nodes, edges = _traverse(registry([]), "A", 3)
    assert nodes == {"A"}
    assert edges == []


def test_wb_be_003_traverse_ignores_an_unrelated_edge():
    nodes, edges = _traverse(registry([{"id": "e1", "source": "X", "target": "Y"}]), "A", 1)
    assert nodes == {"A"}
    assert edges == []


def test_wb_be_004_traverse_discovers_multiple_hops():
    graph = registry([
        {"id": "e1", "source": "A", "target": "B"},
        {"id": "e2", "source": "B", "target": "C"},
    ])
    nodes, edges = _traverse(graph, "A", 2)
    assert nodes == {"A", "B", "C"}
    assert [edge["id"] for edge in edges] == ["e1", "e1", "e2"]


def test_wb_be_005_traverse_caps_requested_hops_at_three():
    graph = registry([
        {"id": "e1", "source": "A", "target": "B"},
        {"id": "e2", "source": "B", "target": "C"},
        {"id": "e3", "source": "C", "target": "D"},
        {"id": "e4", "source": "D", "target": "E"},
    ])
    nodes, _ = _traverse(graph, "A", 99)
    assert nodes == {"A", "B", "C", "D"}
    assert "E" not in nodes


def plan() -> QueryPlan:
    return QueryPlan(original_question="BBCA", queries=["BBCA kinerja"], ticker="BBCA")


@pytest.mark.asyncio
async def test_wb_be_006_specialists_empty_agent_list_returns_empty_collections():
    orchestrator = Orchestrator(type("Engine", (), {})())
    result = await orchestrator._run_specialists("BBCA", 2025, [], [], plan())
    assert result == ({}, [], [], {}, [])


@pytest.mark.asyncio
async def test_wb_be_007_specialists_collect_and_deduplicate_context(monkeypatch):
    async def news(*args, **kwargs):
        return "berita", AgentContext(
            citations=["[1]"],
            sources=[{"source_id": "same", "source_type": "news", "title": "News"}],
            diagnostics={"news": 1},
            graph_paths=["A -> B"],
        )

    async def financial(*args, **kwargs):
        return "laporan", AgentContext(
            citations=["[1]", "[2]"],
            sources=[
                {"source_id": "same", "source_type": "news", "title": "News"},
                {"source_id": "report", "source_type": "financial_report", "title": "Report"},
            ],
            diagnostics={"financial": 1},
            graph_paths=["A -> B", "A -> C"],
        )

    monkeypatch.setattr("app.core.agent.orchestrator.run_news_agent", news)
    monkeypatch.setattr("app.core.agent.orchestrator.run_financial_agent", financial)
    orchestrator = Orchestrator(type("Engine", (), {})())
    answers, citations, sources, diagnostics, paths = await orchestrator._run_specialists(
        "BBCA", 2025, ["news", "financial"], [], plan()
    )
    assert answers == {"news": "berita", "financial": "laporan"}
    assert citations == ["[1]", "[2]"]
    assert [source["source_id"] for source in sources] == ["report", "same"]
    assert diagnostics == {"news": {"news": 1}, "financial": {"financial": 1}}
    assert paths == ["A -> B", "A -> C"]


@pytest.mark.asyncio
async def test_wb_be_008_specialist_dependency_failure_isolated(monkeypatch):
    async def failed(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("app.core.agent.orchestrator.run_news_agent", failed)
    orchestrator = Orchestrator(type("Engine", (), {})())
    answers, citations, sources, diagnostics, paths = await orchestrator._run_specialists(
        "BBCA", 2025, ["news"], [], plan()
    )
    assert answers == {"news": "[error: offline]"}
    assert citations == sources == paths == []
    assert diagnostics == {}


@pytest.mark.asyncio
async def test_wb_be_009_non_agent_context_is_ignored_safely(monkeypatch):
    async def news(*args, **kwargs):
        return "plain", {"unexpected": True}

    monkeypatch.setattr("app.core.agent.orchestrator.run_news_agent", news)
    orchestrator = Orchestrator(type("Engine", (), {})())
    answers, citations, sources, diagnostics, paths = await orchestrator._run_specialists(
        "BBCA", 2025, ["news"], [], plan()
    )
    assert answers == {"news": "plain"}
    assert citations == sources == paths == []
    assert diagnostics == {}


@pytest.mark.asyncio
async def test_wb_be_010_financial_only_agent_context_is_collected(monkeypatch):
    async def financial(*args, **kwargs):
        return "laporan", AgentContext(
            citations=["[1]"],
            sources=[{"source_id": "report", "source_type": "financial_report"}],
        )

    monkeypatch.setattr("app.core.agent.orchestrator.run_financial_agent", financial)
    orchestrator = Orchestrator(type("Engine", (), {})())
    answers, citations, sources, diagnostics, paths = await orchestrator._run_specialists(
        "BBCA", 2025, ["financial"], [], plan()
    )
    assert answers == {"financial": "laporan"}
    assert citations == ["[1]"]
    assert sources[0]["source_id"] == "report"
    assert diagnostics == {"financial": {}}
    assert paths == []


@pytest.mark.asyncio
async def test_wb_be_011_news_only_agent_context_is_collected(monkeypatch):
    async def news(*args, **kwargs):
        return "berita", AgentContext(graph_paths=["BBCA -> Event"])

    monkeypatch.setattr("app.core.agent.orchestrator.run_news_agent", news)
    orchestrator = Orchestrator(type("Engine", (), {})())
    answers, citations, sources, diagnostics, paths = await orchestrator._run_specialists(
        "BBCA", 2025, ["news"], [], plan()
    )
    assert answers == {"news": "berita"}
    assert citations == sources == []
    assert diagnostics == {"news": {}}
    assert paths == ["BBCA -> Event"]
