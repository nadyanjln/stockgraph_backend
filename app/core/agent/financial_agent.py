"""Financial analyst agent."""

from __future__ import annotations

import re

from app.core.agent.common import (
    AgentContext,
    ChatMessage,
    FINANCIAL_MODEL,
    chat_complete,
    specialist_llm_enabled,
)
from app.services.database.graphrag_engine import GraphRAGEngine
from app.services.database.evidence_retriever import retrieve_local_evidence
from app.services.database.retrieval_optimizer import (
    QueryPlan,
    RetrievalConfig,
    make_vector_contexts,
    optimize_contexts,
    prompt_context_block,
    rewrite_queries,
    semantic_retrieval_prompt,
    source_list_from_contexts,
)
from app.services.database.retrieval_debug import log_observability, prompt_stats
from app.utils.logger import get_logger

logger = get_logger("stockgraph.retrieval")

FINANCIAL_SYSTEM = """\
Kamu adalah Financial Analyst spesialis emiten BEI.
Tugasmu: menganalisis fundamental keuangan (revenue, laba, aset, ekuitas, EPS, ROE)
berdasarkan data IDX yang tersimpan di knowledge graph.

Aturan:
- Jawab ringkas dalam Bahasa Indonesia.
- Sajikan angka dengan unit (triliun rupiah / persen).
- Jika ada perbandingan multi-tahun, tampilkan tren YoY.
- Gunakan laporan keuangan terbaru yang tersedia dan sebutkan periodenya.
- Jangan membuat nomor sitasi; Manager Agent akan menempatkan sitasi tervalidasi.
- Gunakan hanya angka dan fakta yang muncul pada context retrieval.
- Jangan memakai pengetahuan luar, asumsi, atau angka yang tidak muncul pada context.
- Pertanyaan saat ini mengalahkan riwayat percakapan. Abaikan riwayat bila ticker,
  emiten, tahun, atau topiknya berbeda.
- Fokus hanya pada metrik yang ditanyakan; jangan menambah rasio, rekomendasi,
  atau interpretasi investasi yang tidak didukung context.
- Jawab maksimal 3 kalimat pendek.
- Jika konteks kosong, jelaskan keterbatasan fundamental secara spesifik.
- Jika context tidak cukup untuk menjawab suatu klaim, tulis bahwa informasi tersebut
  belum ditemukan pada evidence yang tersedia.
"""


def _financial_evidence_brief(ctx: AgentContext) -> str:
    """Return retrieved financial evidence directly, avoiding a redundant summary call."""
    logger.info(
        "specialist_llm_skipped=%s",
        {
            "agent": "financial",
            "reason": "direct_evidence_mode",
            "contexts": len(ctx.snippets),
            "sources": len(ctx.sources),
        },
    )
    return (
        f"Tersedia {len(ctx.sources)} evidence laporan keuangan yang sudah diranking "
        "dan difilter. Gunakan detail evidence pada blok sumber retrieval."
    )


async def retrieve_financial_context(
    engine: GraphRAGEngine,
    question: str,
    year: int,
    limit: int = 8,
    query_plan: QueryPlan | None = None,
) -> AgentContext:
    """Retrieve the latest available financial period plus semantic context."""
    config = RetrievalConfig.from_env()
    query_plan = query_plan or rewrite_queries(question)
    local = retrieve_local_evidence(
        question,
        target_year=year,
        max_hops=config.graph_depth,
        news_limit=config.top_k_graph,
        config=config,
    )
    contexts = [
        item
        for item in local.contexts
        if item.source_type in {"financial_report", "graph_path"}
    ]
    graph_paths = list(local.graph_paths)
    diagnostics = local.diagnostics.as_dict() if local.diagnostics else {}

    financial_year = year
    period = diagnostics.get("financial_period_used", "")
    match = re.search(r"(20\d{2})", str(period))
    if match:
        financial_year = int(match.group(1))

    if financial_year in engine.available_years:
        result = await engine.query(
            semantic_retrieval_prompt(question, query_plan),
            year=financial_year,
            return_context=True,
        )
        if result.context:
            contexts.extend(
                make_vector_contexts(
                    result.context,
                    [
                        source
                        for source in result.sources
                        if source.get("source_type") in {"financial_report", "", None}
                    ],
                    source_type="financial_report",
                    limit=config.top_k_vector,
                )
            )
            diagnostics["vector_chunks_retrieved"] = len(result.context)
            diagnostics.setdefault("retrieval_strategy_used", []).append("graphrag_semantic")
            diagnostics["retrieval_status"] = "semantic_and_provenance_retrieval_success"
        else:
            diagnostics["retrieval_status"] = "semantic_empty_using_provenance_fallback"
    elif contexts:
        diagnostics["retrieval_status"] = "semantic_graph_unavailable_using_provenance_fallback"

    final_contexts, debug = optimize_contexts(question, contexts, config, query_plan)
    sources = source_list_from_contexts(final_contexts)
    context_block = prompt_context_block(final_contexts, config) if final_contexts else ""
    prompt_debug = prompt_stats(
        question=question,
        prompt=context_block,
        contexts=debug["final_prompt_contexts"],
    )
    log_observability(logger, "Final prompt", {"agent": "financial", **prompt_debug})
    observability = {
        "agent": "financial",
        "question": question,
        "query_rewrite": {
            "ticker": debug["ticker"],
            "tickers": debug.get("tickers", []),
            "intent": debug["query_intent"],
            "queries": debug["rewritten_queries"],
        },
        "graph_retrieval_results": debug["graph_retrieval_results"],
        "semantic_retrieval_results": debug["semantic_retrieval_results"],
        "merge_stage": debug["merge_stage"],
        "deduplication_stage": debug["deduplication_stage"],
        "reranker_stage": debug["reranker_stage"],
        "top_k_selection": debug["top_k_selection"],
        "counts": debug["top_k_selection"],
        "final_contexts": debug["final_prompt_contexts"],
        "final_prompt": prompt_debug,
    }
    diagnostics.update(
        {
            "rewritten_queries": debug["rewritten_queries"],
            "merged_context_count": debug["deduped_contexts"],
            "removed_duplicates": debug["removed_duplicates"],
            "reranked_context_count": debug["reranked_contexts"],
            "final_context_count": debug["final_contexts"],
            "final_top_k": debug["top_k_final"],
            "final_prompt_length": len(context_block),
            "rerank_enabled": debug["rerank_enabled"],
            "retrieval_debug": debug if config.debug_rag else {},
            "retrieval_observability": observability,
        }
    )
    snippets = [context_block] if context_block else []

    return AgentContext(
        snippets=snippets,
        citations=[source.get("source_id", "") for source in sources[:6]],
        sources=sources[:6],
        graph_paths=graph_paths,
        diagnostics=diagnostics,
    )


async def run_financial_agent(
    question: str,
    year: int,
    history: list[ChatMessage],
    engine: GraphRAGEngine,
    query_plan: QueryPlan | None = None,
) -> tuple[str, AgentContext]:
    """Answer using financial statement graph context."""
    ctx = await retrieve_financial_context(
        engine,
        question,
        year,
        query_plan=query_plan,
    )
    if not ctx.snippets:
        return (
            "Laporan keuangan yang berhasil diproses belum ditemukan pada corpus; "
            "validasi fundamental menjadi terbatas.",
            ctx,
        )

    context_block = "\n".join(ctx.snippets)
    if not specialist_llm_enabled():
        return (_financial_evidence_brief(ctx), ctx)

    messages: list[ChatMessage] = [
        {"role": "system", "content": FINANCIAL_SYSTEM},
        *history[-6:],
        {"role": "user", "content": (
            f"Pertanyaan: {question}\n\n"
            f"Data fundamental terbaru yang tersedia:\n{context_block}\n\n"
            "Berikan analisis berbasis angka yang langsung menjawab pertanyaan. "
            "Jangan gunakan riwayat jika berbeda ticker atau topik."
        )},
    ]
    answer = await chat_complete(
        messages,
        model=FINANCIAL_MODEL,
        temperature=0.0,
        top_p=1.0,
        max_tokens=512,
        purpose="Financial Analyst Summary",
        caller="app.core.agent.financial_agent.run_financial_agent",
    )
    return (answer, ctx)


__all__ = ["FINANCIAL_SYSTEM", "retrieve_financial_context", "run_financial_agent"]
