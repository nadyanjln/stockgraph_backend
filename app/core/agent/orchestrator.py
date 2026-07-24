"""Runtime orchestrator for StockGraph's multi-agent chat flow."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.core.agent.common import (
    AgentContext,
    ChatMessage,
    trim_history,
)
from app.core.agent.financial_agent import run_financial_agent
from app.core.agent.manager_agent import (
    manager_plan,
    manager_synthesizer_messages,
    stream_synthesis,
    synthesize_answer,
)
from app.core.agent.news_agent import run_news_agent
from app.core.agent.response_formatter import format_rag_response
from app.core.agent.evidence_policy import no_evidence_answer, source_coverage
from app.services.database.retrieval_debug import (
    build_debug_payload,
    export_question_debug,
    log_observability,
)
from app.services.database.graphrag_engine import GraphRAGEngine
from app.services.database.retrieval_optimizer import QueryPlan, rewrite_queries
from app.utils.logger import get_logger

HISTORY_TURNS = 3
logger = get_logger("stockgraph.retrieval")


def _dedupe_sources(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for item in items:
        key = (
            item.get("source_id")
            or item.get("url")
            or item.get("title")
            or item.get("snippet")
            or ""
        ).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)

    source_priority = {
        "financial_report": 0,
        "structured_financial": 1,
        "graph_path": 2,
        "news": 3,
    }
    return sorted(
        output,
        key=lambda item: source_priority.get(item.get("source_type", ""), 2),
    )[:8]


@dataclass
class Session:
    session_id: str
    history: list[ChatMessage] = field(default_factory=list)


class SessionStore:
    """In-memory session store with automatic history trimming."""

    def __init__(self, max_turns: int = HISTORY_TURNS):
        self._sessions: dict[str, Session] = {}
        self._max_turns = max_turns

    def get_or_create(self, session_id: str | None = None) -> Session:
        sid = session_id or str(uuid.uuid4())
        if sid not in self._sessions:
            self._sessions[sid] = Session(session_id=sid)
        return self._sessions[sid]

    def append(self, session_id: str, user_msg: str, ai_msg: str) -> None:
        session = self.get_or_create(session_id)
        session.history.append({"role": "user", "content": user_msg})
        session.history.append({"role": "assistant", "content": ai_msg})
        session.history = trim_history(session.history, self._max_turns)

    def history(self, session_id: str) -> list[ChatMessage]:
        return self.get_or_create(session_id).history

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class Orchestrator:
    """
    Coordinate manager, news, and financial agents without LangChain.

    Graph context retrieval stays in GraphRAG-SDK. Realtime typing is produced by
    OpenAI async streaming and forwarded through the FastAPI WebSocket.
    """

    def __init__(self, engine: GraphRAGEngine, session_store: SessionStore | None = None):
        self._engine = engine
        self.sessions = session_store or SessionStore()

    async def _run_specialists(
        self,
        question: str,
        year: int,
        agents: list[str],
        history: list[ChatMessage],
        query_plan: QueryPlan,
    ) -> tuple[
        dict[str, str],
        list[str],
        list[dict[str, str]],
        dict[str, dict],
        list[str],
    ]:
        tasks = []
        if "news" in agents:
            tasks.append((
                "news",
                run_news_agent(
                    question,
                    year,
                    history,
                    self._engine,
                    query_plan=query_plan,
                ),
            ))
        if "financial" in agents:
            tasks.append((
                "financial",
                run_financial_agent(
                    question,
                    year,
                    history,
                    self._engine,
                    query_plan=query_plan,
                ),
            ))

        results = await asyncio.gather(
            *(task for _, task in tasks),
            return_exceptions=True,
        )

        sub_answers: dict[str, str] = {}
        sub_citations: list[str] = []
        sub_sources: list[dict[str, str]] = []
        diagnostics: dict[str, dict] = {}
        graph_paths: list[str] = []
        for (name, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                sub_answers[name] = f"[error: {result}]"
                continue
            answer, ctx = result
            sub_answers[name] = answer
            if isinstance(ctx, AgentContext):
                sub_citations.extend(ctx.citations)
                sub_sources.extend(ctx.sources)
                diagnostics[name] = ctx.diagnostics
                graph_paths.extend(ctx.graph_paths)

        return (
            sub_answers,
            list(dict.fromkeys(sub_citations))[:8],
            _dedupe_sources(sub_sources),
            diagnostics,
            list(dict.fromkeys(graph_paths))[:16],
        )

    async def run(self, session_id: str, question: str) -> dict:
        """Run the full flow without token streaming."""
        history = self.sessions.history(session_id)
        plan = await manager_plan(question, history, self._engine.available_years)
        query_plan = rewrite_queries(question)
        sub_answers, sub_citations, sub_sources, diagnostics, graph_paths = await self._run_specialists(
            question,
            plan.year,
            plan.agents,
            history,
            query_plan,
        )
        if not sub_sources:
            final_answer = no_evidence_answer(question)
            final_messages: list[ChatMessage] = []
        else:
            messages = manager_synthesizer_messages(
                question, history, sub_answers, sub_citations, sub_sources
            )
            final_messages = messages
            final_answer = await synthesize_answer(messages)
        if os.getenv("DEBUG_RAG", "").strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("llm_response=%s", final_answer)
        self._export_retrieval_debug(
            question=question,
            plan=plan,
            diagnostics=diagnostics,
            sources=sub_sources,
            graph_paths=graph_paths,
            final_answer=final_answer,
            final_messages=final_messages,
        )
        self.sessions.append(session_id, question, final_answer)
        formatted = format_rag_response(final_answer, sub_citations, sub_sources)
        diagnostic_summary = self._diagnostic_summary(
            question, diagnostics, graph_paths, sub_sources
        )
        return {
            "plan": plan,
            "target_year": plan.year,
            "sub_answers": sub_answers,
            "sub_citations": sub_citations,
            "sources": sub_sources,
            "diagnostics": diagnostic_summary,
            "graph_paths": graph_paths,
            "final_answer": final_answer,
            **formatted,
        }

    async def run_stream(
        self,
        session_id: str,
        question: str,
    ) -> AsyncIterator[dict]:
        """Stream manager planning, specialist completion, and final answer tokens."""
        history = self.sessions.history(session_id)

        try:
            yield {
                "type": "progress",
                "stage": "question_understanding",
                "status": "running",
                "label": "Memahami pertanyaan Anda",
            }
            plan = await manager_plan(question, history, self._engine.available_years)
            query_plan = rewrite_queries(question)
            yield {
                "type": "progress",
                "stage": "question_understanding",
                "status": "completed",
                "label": "Memahami pertanyaan Anda",
            }
            yield {
                "type": "progress",
                "stage": "entity_resolution",
                "status": "completed",
                "label": "Mengidentifikasi emiten dan konteks analisis",
            }
            yield {
                "type": "plan",
                "agents": plan.agents,
                "year": plan.year,
            }

            tasks = []
            if "news" in plan.agents:
                yield {
                    "type": "progress",
                    "stage": "news_retrieval",
                    "status": "running",
                    "label": "Mencari berita yang relevan",
                }
                yield {"type": "agent_start", "agent": "news"}
                tasks.append((
                    "news",
                    run_news_agent(
                        question,
                        plan.year,
                        history,
                        self._engine,
                        query_plan=query_plan,
                    ),
                ))
            if "financial" in plan.agents:
                yield {
                    "type": "progress",
                    "stage": "financial_retrieval",
                    "status": "running",
                    "label": "Menelusuri laporan keuangan IDX",
                }
                yield {"type": "agent_start", "agent": "financial"}
                tasks.append((
                    "financial",
                    run_financial_agent(
                        question,
                        plan.year,
                        history,
                        self._engine,
                        query_plan=query_plan,
                    ),
                ))

            yield {
                "type": "progress",
                "stage": "graph_traversal",
                "status": "running",
                "label": "Menghubungkan informasi pada knowledge graph",
            }
            results = await asyncio.gather(
                *(task for _, task in tasks),
                return_exceptions=True,
            )
            sub_answers: dict[str, str] = {}
            sub_citations: list[str] = []
            sub_sources: list[dict[str, str]] = []
            diagnostics: dict[str, dict] = {}
            graph_paths: list[str] = []
            for (name, _), result in zip(tasks, results):
                if isinstance(result, Exception):
                    sub_answers[name] = f"[error: {result}]"
                    yield {
                        "type": "progress",
                        "stage": (
                            "financial_retrieval"
                            if name == "financial"
                            else "news_retrieval"
                        ),
                        "status": "failed",
                        "label": (
                            "Menelusuri laporan keuangan IDX"
                            if name == "financial"
                            else "Mencari berita yang relevan"
                        ),
                    }
                    yield {"type": "agent_done", "agent": name, "preview": str(result)[:120]}
                    continue

                answer, ctx = result
                sub_answers[name] = answer
                if isinstance(ctx, AgentContext):
                    sub_citations.extend(ctx.citations)
                    sub_sources.extend(ctx.sources)
                    diagnostics[name] = ctx.diagnostics
                    graph_paths.extend(ctx.graph_paths)
                yield {
                    "type": "progress",
                    "stage": (
                        "financial_retrieval"
                        if name == "financial"
                        else "news_retrieval"
                    ),
                    "status": "completed",
                    "label": (
                        "Menelusuri laporan keuangan IDX"
                        if name == "financial"
                        else "Mencari berita yang relevan"
                    ),
                }
                yield {
                    "type": "agent_done",
                    "agent": name,
                    "preview": answer[:160] + ("..." if len(answer) > 160 else ""),
                }

            yield {
                "type": "progress",
                "stage": "graph_traversal",
                "status": "completed",
                "label": "Menghubungkan informasi pada knowledge graph",
            }
            yield {
                "type": "progress",
                "stage": "relevance_validation",
                "status": "running",
                "label": "Memeriksa kualitas dan relevansi sumber",
            }
            sub_citations = list(dict.fromkeys(sub_citations))[:8]
            sub_sources = _dedupe_sources(sub_sources)
            graph_paths = list(dict.fromkeys(graph_paths))[:16]
            diagnostic_summary = self._diagnostic_summary(
                question, diagnostics, graph_paths, sub_sources
            )
            yield {
                "type": "progress",
                "stage": "relevance_validation",
                "status": "completed",
                "label": (
                    "Sumber terbatas ditemukan; menggunakan evidence yang tersedia"
                    if not sub_sources
                    else "Memeriksa kualitas dan relevansi sumber"
                ),
            }

            if not sub_sources:
                yield {
                    "type": "progress",
                    "stage": "answer_generation",
                    "status": "running",
                    "label": "Menyusun analisis berbasis evidence",
                }
                final_answer = no_evidence_answer(question)
                if os.getenv("DEBUG_RAG", "").strip().lower() in {"1", "true", "yes", "on"}:
                    logger.info("llm_response=%s", final_answer)
                self._export_retrieval_debug(
                    question=question,
                    plan=plan,
                    diagnostics=diagnostics,
                    sources=[],
                    graph_paths=graph_paths,
                    final_answer=final_answer,
                    final_messages=[],
                )
                self.sessions.append(session_id, question, final_answer)
                formatted = format_rag_response(final_answer, [], [])
                yield {
                    "type": "progress",
                    "stage": "answer_generation",
                    "status": "completed",
                    "label": "Menyusun analisis berbasis evidence",
                }
                yield {
                    "type": "progress",
                    "stage": "citation_preparation",
                    "status": "completed",
                    "label": "Menyiapkan citation sumber",
                }
                yield {
                    "type": "final",
                    "answer": final_answer,
                    "citations": [],
                    **formatted,
                    "year": plan.year,
                    "agents": plan.agents,
                    "diagnostics": diagnostic_summary,
                    "graph_paths": graph_paths,
                }
                return

            yield {
                "type": "progress",
                "stage": "answer_generation",
                "status": "running",
                "label": "Menyusun analisis berbasis evidence",
            }
            messages = manager_synthesizer_messages(
                question,
                history,
                sub_answers,
                sub_citations,
                sub_sources,
            )

            full_answer_parts: list[str] = []
            async for delta in stream_synthesis(messages):
                full_answer_parts.append(delta)
                yield {"type": "token", "delta": delta}

            final_answer = "".join(full_answer_parts).strip()
            if os.getenv("DEBUG_RAG", "").strip().lower() in {"1", "true", "yes", "on"}:
                logger.info("llm_response=%s", final_answer)
            self._export_retrieval_debug(
                question=question,
                plan=plan,
                diagnostics=diagnostics,
                sources=sub_sources,
                graph_paths=graph_paths,
                final_answer=final_answer,
                final_messages=messages,
            )
            self.sessions.append(session_id, question, final_answer)
            yield {
                "type": "progress",
                "stage": "answer_generation",
                "status": "completed",
                "label": "Menyusun analisis berbasis evidence",
            }
            yield {
                "type": "progress",
                "stage": "citation_preparation",
                "status": "running",
                "label": "Menyiapkan citation sumber",
            }
            formatted = format_rag_response(final_answer, sub_citations, sub_sources)
            yield {
                "type": "progress",
                "stage": "citation_preparation",
                "status": "completed",
                "label": "Menyiapkan citation sumber",
            }

            yield {
                "type": "final",
                "answer": final_answer,
                "citations": sub_citations,
                **formatted,
                "year": plan.year,
                "agents": plan.agents,
                "diagnostics": diagnostic_summary,
                "graph_paths": graph_paths,
            }

        except Exception as exc:
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _diagnostic_summary(
        question: str,
        agent_diagnostics: dict[str, dict],
        graph_paths: list[str],
        sources: list[dict[str, str]],
    ) -> dict:
        merged = {
            "query": question,
            "ticker_detected": "",
            "entity_resolved": False,
            "news_found": 0,
            "news_after_relevance_filter": 0,
            "financial_reports_found": 0,
            "financial_chunks_found": 0,
            "graph_nodes_found": 0,
            "graph_edges_traversed": 0,
            "graph_contexts_retrieved": 0,
            "vector_chunks_retrieved": 0,
            "merged_context_count": 0,
            "reranked_context_count": 0,
            "final_context_count": 0,
            "retrieval_strategy_used": [],
            "fallback_reason": None,
            "corpus_status": "unknown",
            "graph_ingestion_status": "unknown",
            "retrieval_status": "unknown",
        }
        for values in agent_diagnostics.values():
            merged["ticker_detected"] = merged["ticker_detected"] or values.get(
                "ticker_detected", ""
            )
            merged["entity_resolved"] = bool(
                merged["entity_resolved"] or values.get("entity_resolved")
            )
            for key in (
                "news_found",
                "news_after_relevance_filter",
                "financial_reports_found",
                "financial_chunks_found",
                "graph_nodes_found",
                "graph_edges_traversed",
                "graph_contexts_retrieved",
                "vector_chunks_retrieved",
                "merged_context_count",
                "reranked_context_count",
                "final_context_count",
            ):
                merged[key] = max(int(merged[key]), int(values.get(key) or 0))
            for strategy in values.get("retrieval_strategy_used", []):
                if strategy not in merged["retrieval_strategy_used"]:
                    merged["retrieval_strategy_used"].append(strategy)
            for key in ("corpus_status", "graph_ingestion_status", "retrieval_status"):
                value = values.get(key)
                if value and value != "unknown":
                    merged[key] = value
        coverage = source_coverage(sources)
        if not coverage["news"] and not coverage["financial"]:
            merged["fallback_reason"] = "no_valid_retrieved_sources"
        merged["source_coverage"] = coverage
        merged["graph_paths_found"] = len(graph_paths)
        if os.getenv("DEBUG_RAG", "").strip().lower() in {"1", "true", "yes", "on"}:
            debug_trace = {
                name: values.get("retrieval_debug", {})
                for name, values in agent_diagnostics.items()
                if values.get("retrieval_debug")
            }
            if debug_trace:
                logger.info(
                    "rag_debug_trace=%s",
                    json.dumps(
                        {
                            "question": question,
                            "agents": debug_trace,
                            "sources": sources,
                            "graph_paths": graph_paths,
                        },
                        ensure_ascii=False,
                    ),
                )
        logger.info("retrieval_diagnostics=%s", json.dumps(merged, ensure_ascii=False))
        return merged

    @staticmethod
    def _export_retrieval_debug(
        *,
        question: str,
        plan,
        diagnostics: dict[str, dict],
        sources: list[dict[str, str]],
        graph_paths: list[str],
        final_answer: str,
        final_messages: list[ChatMessage],
    ) -> None:
        try:
            final_prompt = "\n\n".join(
                f"{message['role'].upper()}:\n{message['content']}"
                for message in final_messages
            )
            payload = build_debug_payload(
                question=question,
                plan=plan,
                agent_diagnostics=diagnostics,
                sources=sources,
                graph_paths=graph_paths,
                answer=final_answer,
                final_prompt=final_prompt,
            )
            path = export_question_debug(payload)
            log_observability(
                logger,
                "Answer validation",
                {
                    "debug_file": str(path),
                    "unsupported_sentences": [
                        row
                        for row in payload.get("answer_validation", [])
                        if row.get("unsupported")
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001 - debug export must not break chat.
            logger.warning("retrieval_debug_export_failed=%s", exc)


__all__ = ["Orchestrator", "SessionStore", "Session", "HISTORY_TURNS"]
