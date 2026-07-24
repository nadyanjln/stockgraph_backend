"""News analyst agent."""

from __future__ import annotations

from app.core.agent.common import (
    AgentContext,
    ChatMessage,
    NEWS_MODEL,
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

NEWS_SYSTEM = """\
Kamu adalah News Analyst spesialis pasar saham Indonesia (BEI).
Tugasmu: menganalisis berita terkini dan sentimennya terhadap emiten BEI berdasarkan
konteks dari knowledge graph. Fokus pada peristiwa, kebijakan, dan tokoh yang relevan.

Aturan:
- Jawab ringkas dalam Bahasa Indonesia.
- Jangan membuat nomor sitasi; Manager Agent akan menempatkan sitasi tervalidasi.
- Gunakan hanya fakta yang muncul pada context retrieval.
- Jangan memakai pengetahuan luar, asumsi, atau data yang tidak muncul pada context.
- Pertanyaan saat ini mengalahkan riwayat percakapan. Abaikan riwayat bila ticker,
  emiten, tahun, atau topiknya berbeda.
- Fokus hanya pada konteks berita yang langsung menjawab pertanyaan; jangan
  membahas berita pasar umum bila tidak terkait langsung dengan emiten.
- Jawab maksimal 3 kalimat pendek.
- Jangan menyatakan seluruh data tidak tersedia bila hanya aspek berita yang terbatas.
- Jika konteks berita kosong, jelaskan keterbatasan berita secara spesifik.
- Jika context tidak cukup untuk menjawab suatu klaim, tulis bahwa informasi tersebut
  belum ditemukan pada evidence yang tersedia.
"""


def _news_evidence_brief(ctx: AgentContext) -> str:
    """Return retrieved news evidence directly, avoiding a redundant summary call."""
    logger.info(
        "specialist_llm_skipped=%s",
        {
            "agent": "news",
            "reason": "direct_evidence_mode",
            "contexts": len(ctx.snippets),
            "sources": len(ctx.sources),
        },
    )
    return (
        f"Tersedia {len(ctx.sources)} evidence berita yang sudah diranking dan "
        "difilter. Gunakan detail evidence pada blok sumber retrieval."
    )


async def retrieve_news_context(
    engine: GraphRAGEngine,
    question: str,
    year: int,
    limit: int = 6,
    query_plan: QueryPlan | None = None,
) -> AgentContext:
    """Combine exact ticker/graph traversal with semantic GraphRAG context."""
    config = RetrievalConfig.from_env()
    query_plan = query_plan or rewrite_queries(question)
    local = retrieve_local_evidence(
        question,
        target_year=year,
        max_hops=config.graph_depth,
        news_limit=max(limit, config.top_k_graph),
        config=config,
    )
    contexts = [
        item
        for item in local.contexts
        if item.source_type in {"news", "graph_path"}
    ]
    graph_paths = list(local.graph_paths)
    diagnostics = local.diagnostics.as_dict() if local.diagnostics else {}

    if year in engine.available_years:
        result = await engine.query(
            semantic_retrieval_prompt(question, query_plan),
            year=year,
            return_context=True,
        )
        if result.context:
            contexts.extend(
                make_vector_contexts(
                    result.context,
                    [
                        source
                        for source in result.sources
                        if source.get("source_type", "news") == "news"
                    ],
                    source_type="news",
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
    log_observability(logger, "Final prompt", {"agent": "news", **prompt_debug})
    observability = {
        "agent": "news",
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
        citations=[source.get("source_id", "") for source in sources[:limit]],
        sources=sources[:limit],
        graph_paths=graph_paths,
        diagnostics=diagnostics,
    )


async def run_news_agent(
    question: str,
    year: int,
    history: list[ChatMessage],
    engine: GraphRAGEngine,
    query_plan: QueryPlan | None = None,
) -> tuple[str, AgentContext]:
    """Answer using news-related graph context."""
    ctx = await retrieve_news_context(
        engine,
        question,
        year,
        query_plan=query_plan,
    )
    if not ctx.snippets:
        return (
            "Berita relevan belum ditemukan pada corpus saat ini; "
            "analisis faktor eksternal dan sentimen menjadi terbatas.",
            ctx,
        )

    context_block = "\n".join(ctx.snippets)
    if not specialist_llm_enabled():
        return (_news_evidence_brief(ctx), ctx)

    messages: list[ChatMessage] = [
        {"role": "system", "content": NEWS_SYSTEM},
        *history[-6:],
        {"role": "user", "content": (
            f"Pertanyaan: {question}\n\n"
            f"Konteks berita (year={year}):\n{context_block}\n\n"
            "Berikan analisis singkat berbasis berita yang langsung menjawab pertanyaan. "
            "Jangan gunakan riwayat jika berbeda ticker atau topik."
        )},
    ]
    answer = await chat_complete(
        messages,
        model=NEWS_MODEL,
        temperature=0.0,
        top_p=1.0,
        max_tokens=512,
        purpose="News Analyst Summary",
        caller="app.core.agent.news_agent.run_news_agent",
    )
    return (answer, ctx)


__all__ = ["NEWS_SYSTEM", "retrieve_news_context", "run_news_agent"]
