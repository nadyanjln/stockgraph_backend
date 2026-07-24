from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.agent.common import AgentContext
from app.core.agent.orchestrator import Orchestrator, _dedupe_sources
from app.services.database.graphrag_engine import GraphRAGEngine
from app.services.database.retrieval_optimizer import rewrite_queries


class _Engine:
    available_years = [2025]


class _FakeRag:
    def __init__(self) -> None:
        self.calls = 0

    async def completion(self, question: str, return_context: bool = True):
        self.calls += 1
        await asyncio.sleep(0.01)
        return SimpleNamespace(answer=question, context=[])


class LlmRequestOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def test_final_sources_prioritize_official_financial_evidence(self) -> None:
        sources = [
            {
                "source_id": f"news:{index}",
                "source_type": "news",
                "title": f"News {index}",
            }
            for index in range(8)
        ]
        sources.append(
            {
                "source_id": "financial:BBRI:2025",
                "source_type": "financial_report",
                "title": "Laporan Keuangan BBRI",
            }
        )

        selected = _dedupe_sources(sources)

        self.assertEqual(selected[0]["source_type"], "financial_report")
        self.assertEqual(len(selected), 8)

    async def test_identical_graphrag_requests_share_one_inflight_call(self) -> None:
        engine = GraphRAGEngine()
        rag = _FakeRag()
        key = (2025, "shared question", True)

        first, second = await asyncio.gather(
            engine._completion_once(rag, key, "shared question", True),
            engine._completion_once(rag, key, "shared question", True),
        )

        self.assertIs(first, second)
        self.assertEqual(rag.calls, 1)

    async def test_orchestrator_builds_one_query_plan_for_both_agents(self) -> None:
        orchestrator = Orchestrator(_Engine())
        manager_plan = SimpleNamespace(
            agents=["news", "financial"],
            year=2025,
            rationale="test",
        )
        query_plan = rewrite_queries("[BBRI] Bagaimana kinerja dan beritanya?")
        specialist_result = ("evidence", AgentContext())

        with (
            patch.dict(os.environ, {"DEBUG_RAG": "false"}, clear=False),
            patch.object(Orchestrator, "_export_retrieval_debug"),
            patch(
                "app.core.agent.orchestrator.manager_plan",
                new=AsyncMock(return_value=manager_plan),
            ),
            patch(
                "app.core.agent.orchestrator.rewrite_queries",
                return_value=query_plan,
            ) as rewrite_mock,
            patch(
                "app.core.agent.orchestrator.run_news_agent",
                new=AsyncMock(return_value=specialist_result),
            ) as news_mock,
            patch(
                "app.core.agent.orchestrator.run_financial_agent",
                new=AsyncMock(return_value=specialist_result),
            ) as financial_mock,
        ):
            await orchestrator.run("request-optimization", "Pertanyaan investor")

        rewrite_mock.assert_called_once_with("Pertanyaan investor")
        self.assertIs(news_mock.await_args.kwargs["query_plan"], query_plan)
        self.assertIs(financial_mock.await_args.kwargs["query_plan"], query_plan)


if __name__ == "__main__":
    unittest.main()
