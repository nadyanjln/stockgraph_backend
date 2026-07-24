from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.agent.orchestrator import Orchestrator


class _Engine:
    available_years = [2026]


class ProgressEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_emits_safe_structured_progress(self) -> None:
        plan = SimpleNamespace(agents=[], year=2026, rationale="internal rationale")
        orchestrator = Orchestrator(_Engine())

        with patch(
            "app.core.agent.orchestrator.manager_plan",
            new=AsyncMock(return_value=plan),
        ), patch.dict(
            os.environ,
            {"DEBUG_RAG": "false"},
            clear=False,
        ), patch.object(Orchestrator, "_export_retrieval_debug"):
            events = [
                event
                async for event in orchestrator.run_stream(
                    "progress-test",
                    "[BBCA] Bagaimana prospeknya?",
                )
            ]

        progress = [event for event in events if event.get("type") == "progress"]
        stages = {(event["stage"], event["status"]) for event in progress}
        self.assertIn(("question_understanding", "running"), stages)
        self.assertIn(("question_understanding", "completed"), stages)
        self.assertIn(("entity_resolution", "completed"), stages)
        self.assertIn(("relevance_validation", "completed"), stages)
        self.assertIn(("answer_generation", "completed"), stages)
        self.assertIn(("citation_preparation", "completed"), stages)

        plan_event = next(event for event in events if event.get("type") == "plan")
        self.assertNotIn("rationale", plan_event)
        self.assertEqual(events[-1]["type"], "final")


if __name__ == "__main__":
    unittest.main()
