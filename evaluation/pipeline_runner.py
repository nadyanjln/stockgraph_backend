"""Runner for executing live GraphRAG pipeline questions in evaluation mode."""

from __future__ import annotations

import logging
from typing import Any

from app.services.database.graphrag_engine import GraphRAGEngine
from app.core.agent.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Wrapper around GraphRAGEngine and Orchestrator to execute live queries."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        llm_model: str = "openai/gpt-4o-mini",
        embedder_model: str = "openai/text-embedding-3-small",
        embedder_dim: int = 256,
    ) -> None:
        self.engine = GraphRAGEngine(
            host=host,
            port=port,
            llm_model=llm_model,
            embedder_model=embedder_model,
            embedder_dim=embedder_dim,
        )
        self.orchestrator: Orchestrator | None = None

    async def initialize(self) -> None:
        """Initialize the GraphRAG Engine and Orchestrator."""
        logger.info("Initializing live GraphRAG Engine...")
        await self.engine.initialize()
        self.orchestrator = Orchestrator(self.engine)
        logger.info("GraphRAG Engine and Orchestrator successfully initialized.")

    async def run_query(self, question: str, session_id: str | None = None) -> dict[str, Any]:
        """Execute a live RAG query and return answer & contexts."""
        if not self.orchestrator:
            raise RuntimeError("PipelineRunner must be initialized by calling initialize() first.")

        sid = session_id or "eval-session"
        # The orchestrator handles routing, retrieval optimization and final synthesis
        res = await self.orchestrator.run(sid, question)

        sources = res.get("sources", [])
        contexts: list[str] = []
        for src in sources:
            txt = src.get("retrieved_text") or src.get("snippet") or ""
            if txt:
                contexts.append(txt)

        return {
            "answer": res.get("final_answer", ""),
            "contexts": contexts,
            "target_year": res.get("target_year"),
            "agents": res.get("plan").agents if res.get("plan") else [],
        }

    async def close(self) -> None:
        """Shutdown connection pool of FalkorDB connection."""
        logger.info("Closing live GraphRAG Engine...")
        await self.engine.close()
