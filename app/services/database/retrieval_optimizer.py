"""Hybrid retrieval scoring, deduplication, reranking, and context budgeting."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

from app.services.database.retrieval_debug import (
    context_token_stats,
    log_observability,
)

logger = logging.getLogger("stockgraph.retrieval")

RetrievalSource = Literal["graph", "vector", "provenance"]


FINANCIAL_KEYWORDS = {
    "aset",
    "capex",
    "cash",
    "debt",
    "dividen",
    "dividend",
    "ebitda",
    "ekuitas",
    "eps",
    "kas",
    "laba",
    "liabilitas",
    "margin",
    "pendapatan",
    "profit",
    "profitabilitas",
    "revenue",
    "roa",
    "roe",
    "utang",
}

NEWS_KEYWORDS = {
    "berita",
    "event",
    "hari",
    "kabar",
    "kebijakan",
    "latest",
    "news",
    "peristiwa",
    "regulasi",
    "sentimen",
    "terbaru",
}

INTENT_KEYWORDS: dict[str, set[str]] = {
    "financial_performance": {
        "kinerja",
        "performance",
        "pendapatan",
        "revenue",
        "laba",
        "earnings",
        "penjualan",
        "sales",
    },
    "risk": {
        "risiko",
        "risk",
        "kompetisi",
        "competition",
        "regulasi",
        "regulation",
        "ketidakpastian",
        "uncertainty",
        "tekanan",
        "pressure",
        "utang",
        "debt",
        "leverage",
        "tantangan",
        "challenge",
        "downside",
    },
    "profitability": {
        "profitabilitas",
        "profitability",
        "roe",
        "roa",
        "margin",
        "eps",
        "npm",
        "laba",
        "profit",
        "income",
    },
    "growth": {
        "pertumbuhan",
        "growth",
        "ekspansi",
        "expansion",
        "naik",
        "meningkat",
        "revenue",
        "pendapatan",
        "earnings",
        "loan",
        "kredit",
    },
    "dividend": {
        "dividen",
        "dividend",
        "payout",
        "retained",
        "earnings",
        "policy",
        "kebijakan",
        "cash",
        "flow",
        "kas",
    },
    "cash_flow": {
        "arus",
        "kas",
        "cash",
        "flow",
        "operasi",
        "operating",
        "capex",
        "free",
        "fcf",
    },
    "balance_sheet": {
        "neraca",
        "balance",
        "sheet",
        "aset",
        "asset",
        "liabilitas",
        "liability",
        "ekuitas",
        "equity",
        "utang",
        "debt",
    },
    "corporate_action": {
        "akuisisi",
        "acquisition",
        "merger",
        "buyback",
        "rights",
        "issue",
        "ipo",
        "divestasi",
        "corporate",
        "action",
    },
    "regulation": {
        "regulasi",
        "regulation",
        "kebijakan",
        "policy",
        "pemerintah",
        "government",
        "tarif",
        "subsidi",
        "izin",
    },
    "industry_competition": {
        "industri",
        "industry",
        "kompetisi",
        "competition",
        "persaingan",
        "market",
        "pangsa",
        "segment",
        "sektor",
    },
    "news": NEWS_KEYWORDS,
    "investment": {
        "investasi",
        "investment",
        "beli",
        "buy",
        "hold",
        "jual",
        "sell",
        "prospek",
        "outlook",
        "menarik",
    },
    "valuation": {
        "valuasi",
        "valuation",
        "per",
        "pbv",
        "ev",
        "ebitda",
        "murah",
        "mahal",
        "harga",
        "price",
    },
}

COMPLEX_KEYWORDS = {
    "analisis",
    "bandingkan",
    "dibandingkan",
    "faktor",
    "prospek",
    "risiko",
    "strategi",
    "valuasi",
}

TICKER_ALIASES = {
    "BBCA": ["bank central asia", "bca"],
    "BBRI": ["bank rakyat indonesia", "bri"],
    "BMRI": ["bank mandiri", "mandiri"],
    "BBNI": ["bank negara indonesia", "bni"],
    "BBTN": ["bank tabungan negara", "btn"],
    "BRIS": ["bank syariah indonesia", "bsi"],
    "TLKM": ["telkom indonesia", "telkom"],
    "UNVR": ["unilever indonesia", "unilever"],
    "ASII": ["astra international", "astra"],
    "ICBP": ["indofood cbp", "icbp"],
    "INDF": ["indofood sukses makmur", "indofood"],
    "ANTM": ["ane ka tambang", "antam"],
    "MDKA": ["merdeka copper gold", "merdeka"],
    "AMMN": ["amman mineral", "amman"],
    "GOTO": ["gojek tokopedia", "goto"],
    "ADRO": ["adaro energy", "adaro"],
    "PTBA": ["bukit asam", "ptba"],
    "EXCL": ["xl axiata", "xl"],
    "ISAT": ["indosat", "indosat ooredoo"],
    "CPIN": ["charoen pokphand indonesia", "charoen pokphand"],
    "SIDO": ["sido muncul", "industri jamu sido muncul"],
    "KLBF": ["kalbe farma", "kalbe"],
    "SMGR": ["semen indonesia", "sig"],
    "PGAS": ["perusahaan gas negara", "pgn"],
}


@dataclass(frozen=True)
class RetrievalConfig:
    top_k_vector: int = 8
    top_k_graph: int = 12
    top_k_final: int = 3
    similarity_threshold: float = 0.70
    semantic_threshold: float = 0.70
    max_context_length: int = 6000
    per_context_max_chars: int = 1200
    rerank_enabled: bool = True
    graph_depth: int = 2
    debug_rag: bool = False
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    min_relevance_score: float = 0.70
    semantic_weight: float = 0.35
    ticker_weight: float = 0.25
    entity_weight: float = 0.20
    intent_weight: float = 0.15
    graph_weight: float = 0.05
    context_focus_enabled: bool = True
    context_focus_max_sentences: int = 2

    @classmethod
    def from_env(cls) -> "RetrievalConfig":
        return cls(
            top_k_vector=_env_int("TOP_K_VECTOR", 8),
            top_k_graph=_env_int("TOP_K_GRAPH", 12),
            top_k_final=_env_int("FINAL_TOP_K", _env_int("TOP_K_FINAL", 3)),
            similarity_threshold=_env_float(
                "SEMANTIC_THRESHOLD",
                _env_float("SIMILARITY_THRESHOLD", 0.70),
            ),
            semantic_threshold=_env_float(
                "SEMANTIC_THRESHOLD",
                _env_float("SIMILARITY_THRESHOLD", 0.70),
            ),
            max_context_length=_env_int("MAX_CONTEXT_LENGTH", 6000),
            per_context_max_chars=_env_int("PER_CONTEXT_MAX_CHARS", 1200),
            rerank_enabled=_env_bool("RERANK_ENABLED", True),
            graph_depth=_env_int("GRAPH_DEPTH", 2),
            debug_rag=_env_bool("DEBUG_RAG", False),
            rerank_model=os.getenv(
                "RERANK_MODEL",
                "cross-encoder/ms-marco-MiniLM-L-6-v2",
            ),
            min_relevance_score=_env_float("MIN_RELEVANCE_SCORE", 0.70),
            semantic_weight=_env_float("RETRIEVAL_WEIGHT_SEMANTIC", 0.35),
            ticker_weight=_env_float("RETRIEVAL_WEIGHT_TICKER", 0.25),
            entity_weight=_env_float("RETRIEVAL_WEIGHT_ENTITY", 0.20),
            intent_weight=_env_float("RETRIEVAL_WEIGHT_INTENT", 0.15),
            graph_weight=_env_float("RETRIEVAL_WEIGHT_GRAPH", 0.05),
            context_focus_enabled=_env_bool("CONTEXT_FOCUS_ENABLED", True),
            context_focus_max_sentences=_env_int("CONTEXT_FOCUS_MAX_SENTENCES", 2),
        )


@dataclass(frozen=True)
class QueryPlan:
    original_question: str
    queries: list[str]
    ticker: str = ""
    tickers: list[str] = field(default_factory=list)
    company_terms: list[str] = field(default_factory=list)
    intent: Literal["simple", "financial", "news", "complex"] = "simple"
    retrieval_intents: list[str] = field(default_factory=list)

    @property
    def search_text(self) -> str:
        return " ".join([self.original_question, *self.queries])


@dataclass
class RetrievedContext:
    text: str
    source_type: str
    retrieval_source: RetrievalSource
    source: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    graph_score: float = 0.0
    vector_score: float = 0.0
    rerank_score: float | None = None
    initial_score: float = 0.0
    source_id: str = ""
    node_id: str = ""
    edge_path: str = ""
    semantic_score: float = 0.0
    provenance_score: float = 0.0
    exact_match_score: float = 0.0
    entity_score: float = 0.0
    ticker_score: float = 0.0
    topic_score: float = 0.0
    year_score: float = 0.0
    financial_keyword_score: float = 0.0
    intent_score: float = 0.0
    recency_score: float = 0.0
    accepted: bool = True
    rejection_reason: str = ""

    def key(self) -> str:
        natural_key = (
            self.source_id
            or str(self.source.get("source_id") or "")
            or str(self.source.get("url") or "")
            or self.node_id
            or self.edge_path
        )
        if natural_key:
            return natural_key.strip().lower()
        normalized = re.sub(r"\s+", " ", self.text.casefold()).strip()[:500]
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def prompt_text(self, index: int, max_chars: int, include_debug: bool = False) -> str:
        label = {
            "news": "Berita",
            "financial_report": "Laporan Keuangan IDX",
            "graph_path": "Relasi Knowledge Graph",
        }.get(self.source_type, self.source_type or "Sumber")
        text = _truncate(self.text, max_chars)
        score = self.rerank_score if self.rerank_score is not None else self.score
        debug = ""
        if include_debug:
            debug = (
                f"\nScore: {score:.3f}; retrieval_source={self.retrieval_source}; "
                f"semantic={self.semantic_score:.3f}; graph={self.graph_score:.3f}; "
                f"provenance={self.provenance_score:.3f}; exact={self.exact_match_score:.3f}; "
                f"financial={self.financial_keyword_score:.3f}; recency={self.recency_score:.3f}; "
                f"rerank={self.rerank_score if self.rerank_score is not None else 0:.3f}"
            )
        return f"[Context {index}] [{label}] {text}{debug}"


def rewrite_queries(question: str) -> QueryPlan:
    """Build deterministic search variants before graph/vector retrieval."""
    tickers = _extract_tickers(question)
    ticker = tickers[0] if tickers else ""
    company_terms = _unique([term for item in tickers for term in _company_terms(item)])
    intent = _classify_intent(question)
    retrieval_intents = _classify_retrieval_intents(question)
    subject = ticker or _subject_phrase(question)

    if not subject:
        subject = "emiten BEI"

    queries = [
        f"{subject} financial performance",
        f"{subject} earnings profitability",
        f"{subject} latest news",
        f"{subject} industry context",
        f"{subject} business strategy",
    ]
    if intent == "news":
        queries = [
            f"{subject} latest news",
            f"{subject} corporate action",
            f"{subject} market sentiment",
            f"{subject} industry policy",
            f"{subject} business impact",
        ]
    elif intent == "financial":
        queries = [
            f"{subject} financial performance",
            f"{subject} revenue net income",
            f"{subject} profitability ROE ROA",
            f"{subject} cash flow debt",
            f"{subject} financial report",
        ]
    elif intent == "complex":
        queries = [
            f"{subject} financial performance",
            f"{subject} latest news",
            f"{subject} industry risk",
            f"{subject} profitability and cash flow",
            f"{subject} business strategy",
        ]
    if retrieval_intents:
        intent_queries = [
            f"{subject} {_intent_query_phrase(item)}"
            for item in retrieval_intents[:2]
            if _intent_query_phrase(item)
        ]
        queries = [*intent_queries, *queries]

    unique_queries = _unique([query.strip() for query in queries if query.strip()])[:5]
    plan = QueryPlan(
        original_question=question,
        queries=unique_queries,
        ticker=ticker,
        tickers=tickers,
        company_terms=company_terms,
        intent=intent,
        retrieval_intents=retrieval_intents,
    )
    logger.info(
        "rewritten_queries=%s",
        {
            "ticker": ticker,
            "tickers": tickers,
            "intent": intent,
            "retrieval_intents": retrieval_intents,
            "queries": unique_queries,
        },
    )
    return plan


def semantic_retrieval_prompt(question: str, query_plan: QueryPlan | None = None) -> str:
    """Build one shared semantic retrieval prompt for all specialist agents."""
    plan = query_plan or rewrite_queries(question)
    return (
        "Ambil konteks paling relevan untuk menjawab pertanyaan saham berikut. "
        "Prioritaskan konteks yang menyebut ticker/perusahaan target secara langsung "
        "dan benar-benar menjawab pertanyaan, bukan berita pasar umum. "
        "Sertakan evidence dari laporan keuangan IDX, data fundamental terstruktur, "
        "berita relevan, dan relasi knowledge graph bila tersedia. "
        "Untuk laporan keuangan, fokus pada revenue, laba bersih, aset, ekuitas, EPS, "
        "ROE, arus kas, utang, tren tahunan, sumber IDX, dan periode laporan. "
        "Untuk berita, fokus pada peristiwa, kebijakan, tokoh, sentimen, URL sumber, "
        "dan dampaknya ke emiten. Abaikan konteks perusahaan lain kecuali pertanyaan "
        "meminta perbandingan. Gunakan variasi query berikut untuk memperluas retrieval: "
        f"{'; '.join(plan.queries)}. Pertanyaan: {question}"
    )


def optimize_contexts(
    question: str,
    items: list[RetrievedContext],
    config: RetrievalConfig | None = None,
    query_plan: QueryPlan | None = None,
) -> tuple[list[RetrievedContext], dict[str, Any]]:
    cfg = config or RetrievalConfig.from_env()
    plan = query_plan or rewrite_queries(question)
    log_observability(logger, "Question", {"question": question})
    log_observability(
        logger,
        "Query rewrite result",
        {
            "ticker": plan.ticker,
            "tickers": plan.tickers,
            "intent": plan.intent,
            "retrieval_intents": plan.retrieval_intents,
            "queries": plan.queries,
        },
    )
    scored_all = [_score_context(item, plan, cfg) for item in items]
    log_observability(
        logger,
        "Initial retrieval candidates",
        [
            _candidate_decision_debug(
                item,
                accepted=True,
                reason="retrieved before deduplication and hard filtering",
            )
            for item in scored_all
        ],
    )
    graph_results = [_debug_item(item) for item in scored_all if item.retrieval_source != "vector"]
    semantic_results = [_debug_item(item) for item in scored_all if item.retrieval_source == "vector"]
    log_observability(logger, "Graph retrieval results", graph_results)
    log_observability(logger, "Semantic retrieval results", semantic_results)
    log_observability(
        logger,
        "Merge stage",
        {
            "number_of_contexts": len(scored_all),
            "contexts": [_merge_debug_item(index, item) for index, item in enumerate(scored_all, start=1)],
        },
    )
    deduped, dedup_stats = deduplicate_contexts_with_stats(scored_all)
    filtered, hard_filter_stats = hard_filter_contexts(deduped, cfg, plan)
    reranked = rerank_contexts(question, filtered, cfg, plan)
    final_limit = min(len(reranked), cfg.top_k_final)
    selected = reranked[:final_limit]
    focused = focus_contexts(selected, plan, cfg)
    final = apply_context_budget(focused, cfg)
    log_observability(
        logger,
        "Top-K selection",
        {
            "retrieved": len(items),
            "merged": len(scored_all),
            "deduplicated": len(deduped),
            "hard_filtered": len(filtered),
            "reranked": len(reranked),
            "sent_to_gpt": len(final),
            "configured_final_top_k": cfg.top_k_final,
        },
    )
    debug = {
        "rewritten_queries": plan.queries,
        "query_intent": plan.intent,
        "retrieval_intents": plan.retrieval_intents,
        "ticker": plan.ticker,
        "tickers": plan.tickers,
        "input_contexts": len(items),
        "scored_contexts": len(scored_all),
        "rejected_contexts": hard_filter_stats["rejected_contexts"],
        "deduped_contexts": len(deduped),
        "hard_filtered_contexts": len(filtered),
        "hard_filter_stage": hard_filter_stats,
        "removed_duplicates": dedup_stats,
        "reranked_contexts": len(reranked),
        "final_contexts": len(final),
        "rerank_enabled": cfg.rerank_enabled,
        "similarity_threshold": cfg.similarity_threshold,
        "semantic_threshold": cfg.semantic_threshold,
        "min_relevance_score": cfg.min_relevance_score,
        "top_k_final": final_limit,
        "configured_top_k_final": cfg.top_k_final,
        "scoring_weights": _scoring_weights(cfg),
        "context_focus_enabled": cfg.context_focus_enabled,
        "context_focus_max_sentences": cfg.context_focus_max_sentences,
        "max_context_length": cfg.max_context_length,
        "graph_retrieval_results": graph_results,
        "semantic_retrieval_results": semantic_results,
        "merge_stage": {
            "number_of_contexts": len(scored_all),
            "contexts": [_merge_debug_item(index, item) for index, item in enumerate(scored_all, start=1)],
        },
        "deduplication_stage": dedup_stats,
        "reranker_stage": [_rerank_debug_item(index, item) for index, item in enumerate(reranked, start=1)],
        "threshold_stage": hard_filter_stats,
        "top_k_selection": {
            "retrieved": len(items),
            "merged": len(scored_all),
            "deduplicated": len(deduped),
            "hard_filtered": len(filtered),
            "reranked": len(reranked),
            "sent_to_gpt": len(final),
            "configured_final_top_k": cfg.top_k_final,
        },
        "merged_context_debug": [_debug_item(item) for item in deduped[: cfg.top_k_final]],
        "reranked_context_debug": [_debug_item(item) for item in reranked[: cfg.top_k_final]],
        "final_context_debug": [_debug_item(item) for item in final],
        "final_prompt_contexts": [_debug_item(item) for item in final],
        "final_context_token_stats": context_token_stats([_debug_item(item) for item in final]),
    }
    logger.info(
        "retrieval_selection=%s",
        {
            "ticker": plan.ticker,
            "intent": plan.intent,
            "input": len(items),
            "scored": len(scored_all),
            "deduped": len(deduped),
            "hard_filtered": len(filtered),
            "removed_duplicates": dedup_stats["total_removed"],
            "final_top_k": final_limit,
        },
    )
    if cfg.debug_rag:
        logger.info("hybrid_retrieval_debug=%s", debug)
    return final, debug


def deduplicate_contexts(items: list[RetrievedContext]) -> list[RetrievedContext]:
    deduped, _stats = deduplicate_contexts_with_stats(items)
    return deduped


def deduplicate_contexts_with_stats(
    items: list[RetrievedContext],
) -> tuple[list[RetrievedContext], dict[str, Any]]:
    best_by_key: dict[str, RetrievedContext] = {}
    text_representatives: list[RetrievedContext] = []
    removed = {
        "url": 0,
        "article_fingerprint": 0,
        "entity": 0,
        "financial_report": 0,
        "text_similarity": 0,
        "normalized_text": 0,
        "title_similarity": 0,
        "cosine_similarity": 0,
        "entity_overlap": 0,
    }
    removed_details: list[dict[str, Any]] = []

    for item in sorted(items, key=_rank_key, reverse=True):
        key_type, key = _dedupe_key(item)
        if key in best_by_key:
            removed[key_type] = removed.get(key_type, 0) + 1
            removed_details.append(
                {
                    "removed_id": _context_debug_id(item),
                    "duplicate_of": _context_debug_id(best_by_key[key]),
                    "reason": _dedupe_reason(key_type),
                    "duplicate": True,
                    "accepted": False,
                    "result": f"REJECT ({_dedupe_reason(key_type)})",
                }
            )
            if item.score > best_by_key[key].score:
                best_by_key[key] = item
            continue
        similar_to, similarity_reason = _similar_context(item, text_representatives)
        if similar_to is not None:
            removed[similarity_reason] = removed.get(similarity_reason, 0) + 1
            removed_details.append(
                {
                    "removed_id": _context_debug_id(item),
                    "duplicate_of": _context_debug_id(similar_to),
                    "reason": _dedupe_reason(similarity_reason),
                    "duplicate": True,
                    "accepted": False,
                    "result": f"REJECT ({_dedupe_reason(similarity_reason)})",
                }
            )
            continue
        best_by_key[key] = item
        text_representatives.append(item)

    deduped = sorted(best_by_key.values(), key=_rank_key, reverse=True)
    stats = {
        **removed,
        "total_removed": sum(removed.values()),
        "kept": len(deduped),
        "removed_contexts": removed_details,
    }
    log_observability(
        logger,
        "Deduplication stage",
        {
            "contexts_before_dedup": len(items),
            "contexts_after_dedup": len(deduped),
            "removed_contexts": removed_details,
        },
    )
    return deduped, stats


def hard_filter_contexts(
    items: list[RetrievedContext],
    config: RetrievalConfig,
    plan: QueryPlan,
) -> tuple[list[RetrievedContext], dict[str, Any]]:
    """Drop low-quality candidates before reranking and log every decision."""
    accepted: list[RetrievedContext] = []
    decisions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for index, item in enumerate(items, start=1):
        is_accepted, reason = _hard_filter_item(item, config, plan)
        item.accepted = is_accepted
        item.rejection_reason = "" if is_accepted else reason
        decision = _candidate_decision_debug(
            item,
            accepted=is_accepted,
            reason=reason,
            candidate_index=index,
        )
        decisions.append(decision)
        if is_accepted:
            accepted.append(item)
        else:
            rejected.append(decision)

    log_observability(
        logger,
        "Hard filtering",
        {
            "semantic_threshold": config.semantic_threshold,
            "contexts_before_filter": len(items),
            "contexts_after_filter": len(accepted),
            "decisions": decisions,
        },
    )
    return accepted, {
        "semantic_threshold": config.semantic_threshold,
        "contexts_before_filter": len(items),
        "contexts_after_filter": len(accepted),
        "accepted_contexts": [
            decision for decision in decisions if decision.get("accepted")
        ],
        "rejected_contexts": rejected,
        "decisions": decisions,
    }


def rerank_contexts(
    question: str,
    items: list[RetrievedContext],
    config: RetrievalConfig,
    query_plan: QueryPlan | None = None,
) -> list[RetrievedContext]:
    if not items:
        return []
    plan = query_plan or rewrite_queries(question)
    if not config.rerank_enabled:
        reranked = sorted(items, key=_rank_key, reverse=True)
        log_observability(
            logger,
            "Reranker stage",
            {
                "rerank_enabled": False,
                "contexts": [
                    _rerank_debug_item(index, item)
                    for index, item in enumerate(reranked, start=1)
                ],
            },
        )
        return reranked

    cross_scores = _cross_encoder_scores(plan.search_text, items, config.rerank_model)
    for item, cross_score in zip(items, cross_scores):
        original_score = item.initial_score or item.score
        semantic_score = max(item.semantic_score, cross_score)
        item.rerank_score = _weighted_rerank_score(item, semantic_score, config)
        item.score = item.rerank_score
        item.initial_score = original_score

    reranked = sorted(items, key=_rank_key, reverse=True)
    log_observability(
        logger,
        "Reranker stage",
        [_rerank_debug_item(index, item) for index, item in enumerate(reranked, start=1)],
    )
    return reranked


def dynamic_top_k(
    question: str,
    items: list[RetrievedContext],
    config: RetrievalConfig,
    query_plan: QueryPlan | None = None,
) -> int:
    if not items:
        return 0
    _ = query_plan or rewrite_queries(question)
    confidence = _retrieval_confidence(items)
    if confidence == "high":
        desired = 3
    elif confidence == "medium":
        desired = 4
    else:
        desired = 5
    return max(1, min(len(items), config.top_k_final, desired))


def select_diverse_contexts(
    items: list[RetrievedContext],
    limit: int,
) -> list[RetrievedContext]:
    """Prefer diverse evidence types without keeping weak duplicate facts."""
    if limit <= 0:
        return []
    selected: list[RetrievedContext] = []
    selected_ids: set[str] = set()

    for group in ("financial_report", "structured_financial", "news", "graph_path"):
        match = next(
            (
                item
                for item in items
                if item.source_type == group and _context_debug_id(item) not in selected_ids
            ),
            None,
        )
        if match is not None:
            selected.append(match)
            selected_ids.add(_context_debug_id(match))
        if len(selected) >= limit:
            break

    for item in items:
        key = _context_debug_id(item)
        if key in selected_ids:
            continue
        if any(_text_cosine_similarity(item.text, kept.text) > 0.88 for kept in selected):
            continue
        selected.append(item)
        selected_ids.add(key)
        if len(selected) >= limit:
            break

    return sorted(selected, key=_rank_key, reverse=True)


def focus_contexts(
    items: list[RetrievedContext],
    plan: QueryPlan,
    config: RetrievalConfig,
) -> list[RetrievedContext]:
    if not config.context_focus_enabled:
        return items
    for item in items:
        focused = _focus_context_text(item.text, plan, config.context_focus_max_sentences)
        if focused:
            item.text = focused
    return items


def _apply_minimum_score(
    items: list[RetrievedContext],
    config: RetrievalConfig,
) -> list[RetrievedContext]:
    kept = [item for item in items if item.score >= config.min_relevance_score]
    removed = [
        _candidate_decision_debug(item, accepted=False, reason="below minimum relevance score")
        for item in items
        if item.score < config.min_relevance_score
    ]
    log_observability(
        logger,
        "Minimum score threshold",
        {
            "threshold": config.min_relevance_score,
            "kept": len(kept),
            "removed": removed,
        },
    )
    return kept


def apply_context_budget(
    items: list[RetrievedContext],
    config: RetrievalConfig,
) -> list[RetrievedContext]:
    output: list[RetrievedContext] = []
    total = 0
    for item in items:
        text = _truncate(item.text, config.per_context_max_chars)
        if not text:
            continue
        projected = total + len(text)
        if output and projected > config.max_context_length:
            break
        item.text = text
        output.append(item)
        total += len(text)
    logger.info("final_prompt_context_length=%s", total)
    return output


def prompt_context_block(items: list[RetrievedContext], config: RetrievalConfig) -> str:
    sections = [
        ("Financial Data", {"financial_report", "structured_financial"}),
        ("Latest News", {"news"}),
        ("Knowledge Graph", {"graph_path"}),
        ("Evidence", set()),
    ]
    used: set[int] = set()
    lines: list[str] = ["========================"]
    context_index = 1
    fact_fingerprints: set[str] = set()

    for title, source_types in sections:
        section_items = [
            (idx, item)
            for idx, item in enumerate(items)
            if idx not in used
            and (item.source_type in source_types if source_types else item.source_type not in _known_section_types())
        ]
        if not section_items:
            continue
        lines.append(title)
        for idx, item in section_items:
            used.add(idx)
            clean_text = _dedupe_facts(item.text, fact_fingerprints)
            if not clean_text:
                continue
            item.text = clean_text
            lines.append(item.prompt_text(context_index, config.per_context_max_chars, config.debug_rag))
            context_index += 1
        lines.append("")

    lines.append("========================")
    block = "\n".join(lines).strip()
    logger.info("final_prompt_length=%s", len(block))
    return block


def make_vector_contexts(
    contexts: list[str],
    sources: list[dict[str, str]],
    *,
    source_type: str,
    limit: int,
) -> list[RetrievedContext]:
    output: list[RetrievedContext] = []
    for index, text in enumerate(contexts[:limit]):
        source = sources[index] if index < len(sources) else {}
        output.append(
            RetrievedContext(
                text=str(text),
                source_type=source.get("source_type") or source_type,
                retrieval_source="vector",
                source=source,
                vector_score=0.55,
                source_id=str(source.get("source_id") or ""),
            )
        )
    return output


def source_list_from_contexts(items: list[RetrievedContext]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not item.source:
            continue
        key = (
            str(item.source.get("source_id") or "")
            or str(item.source.get("url") or "")
            or str(item.source.get("title") or "")
            or item.key()
        ).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        source = dict(item.source)
        source.setdefault("retrieval_source", item.retrieval_source)
        source.setdefault("relevance_score", f"{item.score:.4f}")
        source.setdefault("semantic_score", f"{item.semantic_score:.4f}")
        source.setdefault("graph_score", f"{item.graph_score:.4f}")
        source.setdefault("provenance_score", f"{item.provenance_score:.4f}")
        if item.rerank_score is not None:
            source.setdefault("rerank_score", f"{item.rerank_score:.4f}")
        sources.append(source)
    return sources


def _score_context(
    item: RetrievedContext,
    plan: QueryPlan,
    config: RetrievalConfig,
) -> RetrievedContext:
    text_blob = _context_blob(item)
    semantic = _semantic_relevance(plan, text_blob)
    graph = _graph_relevance(item)
    provenance = _provenance_confidence(item)
    ticker = _ticker_match(plan, item)
    entity = _entity_match(plan, item)
    topic = _topic_match(plan, text_blob)
    intent = _intent_match(plan, text_blob)
    year = _year_match(plan, item)
    exact = max(ticker, entity)
    financial = _financial_keyword_match(plan.search_text, text_blob)
    recency = _recency_bonus(item)

    item.semantic_score = semantic
    item.graph_score = max(item.graph_score, graph)
    item.provenance_score = provenance
    item.exact_match_score = exact
    item.entity_score = entity
    item.ticker_score = ticker
    item.topic_score = topic
    item.intent_score = intent
    item.year_score = year
    item.financial_keyword_score = financial
    item.recency_score = recency
    item.score = _weighted_final_score(
        semantic_score=semantic,
        ticker_score=ticker,
        entity_score=entity,
        intent_score=intent,
        graph_score=item.graph_score,
        config=config,
    )
    item.initial_score = item.score
    if item.retrieval_source == "vector":
        item.vector_score = max(item.vector_score, semantic)
    return item


def lexical_overlap(question: str, text: str) -> float:
    query_terms = _terms(question)
    if not query_terms:
        return 0.0
    text_terms = _terms(text)
    if not text_terms:
        return 0.0
    overlap = len(query_terms.intersection(text_terms))
    return _normalize(overlap / max(1, len(query_terms)))


def _semantic_relevance(plan: QueryPlan, text: str) -> float:
    scores = [lexical_overlap(plan.original_question, text)]
    scores.extend(lexical_overlap(query, text) for query in plan.queries)
    return max(scores or [0.0])


def _ticker_match(plan: QueryPlan, item: RetrievedContext) -> float:
    expected_tickers = set(plan.tickers or ([plan.ticker] if plan.ticker else []))
    if not expected_tickers:
        return 0.50 if not _item_ticker(item) else 0.70
    item_ticker = _item_ticker(item)
    if item_ticker in expected_tickers:
        return 1.0
    if item_ticker and item_ticker not in expected_tickers:
        return 0.0
    haystack = _context_blob(item).casefold()
    if any(term.casefold() in haystack for term in plan.company_terms):
        return 0.95
    return 0.0


def _entity_match(plan: QueryPlan, item: RetrievedContext) -> float:
    if _ticker_match(plan, item) >= 0.85:
        return 1.0
    context_blob = _context_blob(item)
    context_entities = _entity_terms(context_blob)
    context_entities.update(_concept_terms(context_blob, plan))
    query_entities = set(plan.company_terms)
    query_entities.update(_entity_terms(plan.original_question))
    query_entities.update(_concept_terms(plan.original_question, plan))
    query_entities.update(plan.tickers)
    if not query_entities:
        return 0.40
    normalized_query = {_normalize_entity(entity) for entity in query_entities if entity}
    normalized_context = {_normalize_entity(entity) for entity in context_entities if entity}
    if not normalized_context:
        return 0.0
    overlap = normalized_query.intersection(normalized_context)
    if overlap:
        return _normalize(len(overlap) / max(1, min(3, len(normalized_query))))
    return 0.0


def _topic_match(plan: QueryPlan, text: str) -> float:
    query_terms = _terms(plan.search_text) - set(plan.company_terms)
    query_terms -= {term.casefold() for term in plan.company_terms}
    if not query_terms:
        return 0.30
    text_terms = _terms(text)
    overlap = query_terms.intersection(text_terms)
    score = len(overlap) / max(1, min(10, len(query_terms)))
    if plan.intent == "financial" and text_terms.intersection(FINANCIAL_KEYWORDS):
        score += 0.20
    if plan.intent == "news" and text_terms.intersection(NEWS_KEYWORDS):
        score += 0.20
    return _normalize(score)


def _intent_match(plan: QueryPlan, text: str) -> float:
    text_terms = _terms(text)
    if not text_terms:
        return 0.0
    if not plan.retrieval_intents:
        return _topic_match(plan, text)

    scores: list[float] = []
    for intent in plan.retrieval_intents:
        keywords = INTENT_KEYWORDS.get(intent, set())
        if not keywords:
            continue
        overlap = text_terms.intersection(keywords)
        score = len(overlap) / max(1, min(8, len(keywords)))
        if overlap:
            score += 0.20
        scores.append(_normalize(score))
    return max(scores or [0.0])


def _year_match(plan: QueryPlan, item: RetrievedContext) -> float:
    question_year = _question_year(plan.original_question)
    if question_year is None:
        return 0.40
    item_year = _item_year(item)
    if item_year and str(question_year) == str(item_year):
        return 1.0
    return 0.0


@lru_cache(maxsize=1)
def _load_cross_encoder(model_name: str):
    try:
        from sentence_transformers import CrossEncoder
    except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
        logger.info("CrossEncoder reranker unavailable; using feature fallback (%s)", exc)
        return None
    try:
        return CrossEncoder(model_name)
    except Exception as exc:  # noqa: BLE001 - model may be unavailable offline.
        logger.warning("CrossEncoder reranker failed to load; using feature fallback (%s)", exc)
        return None


def _cross_encoder_scores(
    question: str,
    items: list[RetrievedContext],
    model_name: str,
) -> list[float]:
    model = _load_cross_encoder(model_name)
    if model is None:
        return [lexical_overlap(question, item.text) for item in items]
    pairs = [(question, item.text) for item in items]
    raw_scores = model.predict(pairs)
    values = [float(score) for score in raw_scores]
    if not values:
        return []
    minimum = min(values)
    maximum = max(values)
    if maximum == minimum:
        return [0.5 for _ in values]
    return [_normalize((score - minimum) / (maximum - minimum)) for score in values]


def _feature_rerank_score(item: RetrievedContext, plan: QueryPlan) -> float:
    support = item.semantic_score
    factual_density = _factual_density(item.text)
    financial_importance = max(item.financial_keyword_score, _financial_keyword_match("", item.text))
    source_priority = _source_priority(item, plan)
    exact = item.exact_match_score
    return _normalize(
        (support * 0.30)
        + (financial_importance * 0.25)
        + (source_priority * 0.20)
        + (factual_density * 0.15)
        + (exact * 0.10)
    )


def _weighted_rerank_score(
    item: RetrievedContext,
    semantic_score: float,
    config: RetrievalConfig,
) -> float:
    intent_score = max(item.intent_score, item.topic_score * 0.65)
    return _weighted_final_score(
        semantic_score=semantic_score,
        ticker_score=item.ticker_score,
        entity_score=item.entity_score,
        intent_score=intent_score,
        graph_score=item.graph_score,
        config=config,
    )


def _weighted_final_score(
    *,
    semantic_score: float,
    ticker_score: float,
    entity_score: float,
    intent_score: float,
    graph_score: float,
    config: RetrievalConfig,
) -> float:
    weights = _scoring_weights(config)
    total_weight = sum(weights.values()) or 1.0
    weighted = (
        semantic_score * weights["semantic"]
        + ticker_score * weights["ticker"]
        + entity_score * weights["entity"]
        + intent_score * weights["intent"]
        + graph_score * weights["graph"]
    )
    return _normalize(weighted / total_weight)


def _scoring_weights(config: RetrievalConfig) -> dict[str, float]:
    return {
        "semantic": max(0.0, config.semantic_weight),
        "ticker": max(0.0, config.ticker_weight),
        "entity": max(0.0, config.entity_weight),
        "intent": max(0.0, config.intent_weight),
        "graph": max(0.0, config.graph_weight),
    }


def _candidate_filter(item: RetrievedContext, plan: QueryPlan) -> tuple[bool, str]:
    item_ticker = _item_ticker(item)
    expected_tickers = set(plan.tickers or ([plan.ticker] if plan.ticker else []))
    if expected_tickers and item_ticker and item_ticker not in expected_tickers:
        return False, f"ticker mismatch: expected {sorted(expected_tickers)}, got {item_ticker}"

    same_ticker = item.ticker_score >= 0.85
    same_entity = item.entity_score >= 0.50
    same_topic = max(item.topic_score, item.intent_score) >= 0.35
    same_year = item.year_score >= 0.90

    if item.source_type == "news" and not (same_ticker or same_entity or same_topic):
        return False, "generic market news without direct ticker/entity/topic match"

    if same_ticker or same_entity or same_topic or same_year:
        return True, "accepted"
    return False, "no ticker/entity/topic/year alignment"


def _hard_filter_item(
    item: RetrievedContext,
    config: RetrievalConfig,
    plan: QueryPlan,
) -> tuple[bool, str]:
    expected_tickers = set(plan.tickers or ([plan.ticker] if plan.ticker else []))
    item_ticker = _item_ticker(item)
    context_tickers = _context_tickers(item)

    if expected_tickers and item_ticker and item_ticker not in expected_tickers:
        return False, f"ticker mismatch: expected {sorted(expected_tickers)}, got {item_ticker}"

    if expected_tickers and context_tickers and not context_tickers.intersection(expected_tickers):
        return False, f"entity mismatch: context tickers {sorted(context_tickers)}"

    if expected_tickers and item.ticker_score < 0.85 and item.entity_score < 0.50:
        return False, "entity mismatch: no requested ticker/company evidence"

    if item.semantic_score < config.semantic_threshold:
        return False, f"semantic threshold: {item.semantic_score:.3f} < {config.semantic_threshold:.3f}"

    if item.score < config.min_relevance_score:
        return False, f"weighted relevance threshold: {item.score:.3f} < {config.min_relevance_score:.3f}"

    if item.source_type == "news" and _is_generic_market_context(item, plan):
        return False, "generic market news without direct support for the question"

    if not _has_supporting_evidence(item, plan):
        return False, "weak supporting evidence"

    return True, "accepted"


def _candidate_decision_debug(
    item: RetrievedContext,
    *,
    accepted: bool,
    reason: str,
    candidate_index: int | None = None,
) -> dict[str, Any]:
    payload = {
        "id": _context_debug_id(item),
        "source": _source_label(item),
        "ticker": _item_ticker(item),
        "type": item.source_type,
        "semantic_score": item.semantic_score,
        "graph_score": item.graph_score,
        "entity_score": item.entity_score,
        "ticker_score": item.ticker_score,
        "intent_score": item.intent_score,
        "ticker_match": item.ticker_score >= 0.85,
        "entity_match": item.entity_score >= 0.50,
        "topic_score": item.topic_score,
        "year_score": item.year_score,
        "final_score": item.score,
        "duplicate": False,
        "accepted": accepted,
        "result": "ACCEPT" if accepted else f"REJECT ({reason})",
        "reason": reason,
    }
    if candidate_index is not None:
        payload["candidate"] = candidate_index
    return payload


def _retrieval_confidence(items: list[RetrievedContext]) -> Literal["high", "medium", "low"]:
    if not items:
        return "low"
    top_score = items[0].score
    average_top = sum(item.score for item in items[: min(3, len(items))]) / min(3, len(items))
    if top_score >= 0.88 and average_top >= 0.82:
        return "high"
    if top_score >= 0.78 and average_top >= 0.72:
        return "medium"
    return "low"


def _source_priority(item: RetrievedContext, plan: QueryPlan) -> float:
    if item.source_type == "financial_report":
        return 1.0
    if item.source_type == "structured_financial":
        return 0.95
    if item.source_type == "graph_path":
        return 0.78
    if item.source_type == "news":
        return 0.78 if plan.intent == "news" else 0.48
    return 0.45


def _context_tickers(item: RetrievedContext) -> set[str]:
    blob = _context_blob(item)
    lowered = blob.casefold()
    tickers: set[str] = set()
    item_ticker = _item_ticker(item)
    if item_ticker in TICKER_ALIASES:
        tickers.add(item_ticker)
    for ticker, aliases in TICKER_ALIASES.items():
        if re.search(rf"\b{re.escape(ticker)}\b", blob):
            tickers.add(ticker)
            continue
        if any(alias in lowered for alias in aliases):
            tickers.add(ticker)
    return tickers


def _is_generic_market_context(item: RetrievedContext, plan: QueryPlan) -> bool:
    if item.source_type != "news":
        return False
    direct_company_match = item.ticker_score >= 0.85 or item.entity_score >= 0.50
    direct_topic_match = max(item.topic_score, item.intent_score) >= 0.35
    news_terms = _terms(_context_blob(item)).intersection(NEWS_KEYWORDS)
    if plan.intent == "news" and direct_company_match and direct_topic_match:
        return False
    if direct_company_match and (direct_topic_match or news_terms):
        return False
    return True


def _has_supporting_evidence(item: RetrievedContext, plan: QueryPlan) -> bool:
    blob = _context_blob(item)
    terms = _terms(blob)
    if len(terms) < 8:
        return False

    factual_density = _factual_density(blob)
    has_financial_signal = item.financial_keyword_score >= 0.12
    has_topic_signal = max(item.topic_score, item.intent_score) >= 0.20
    has_numbers = bool(re.search(r"(?:rp\s*)?\d+(?:[.,]\d+)?%?|20\d{2}", blob.casefold()))
    has_entity_signal = item.ticker_score >= 0.85 or item.entity_score >= 0.50
    has_news_signal = bool(terms.intersection(NEWS_KEYWORDS))

    if has_entity_signal and (has_topic_signal or has_financial_signal or has_numbers):
        return True
    if item.source_type in {"financial_report", "structured_financial"} and (
        has_financial_signal or has_numbers or factual_density >= 0.18
    ):
        return True
    if item.source_type == "graph_path" and has_entity_signal and has_topic_signal:
        return True
    if plan.intent == "news" and item.source_type == "news" and has_entity_signal and has_news_signal:
        return True
    return False


def _focus_context_text(text: str, plan: QueryPlan, max_sentences: int) -> str:
    clean = " ".join(str(text or "").split())
    if not clean or max_sentences <= 0:
        return clean

    sentences = _sentences(clean)
    if len(sentences) <= 1:
        return clean

    query_terms = _terms(plan.search_text)
    company_terms = {term.casefold() for term in plan.company_terms}
    ticker_terms = {ticker.casefold() for ticker in plan.tickers}
    non_company_query_terms = query_terms - company_terms - ticker_terms
    intent_terms: set[str] = set()
    for intent in plan.retrieval_intents:
        intent_terms.update(INTENT_KEYWORDS.get(intent, set()))
    important_terms = query_terms.union(company_terms).union(intent_terms).union(FINANCIAL_KEYWORDS)

    scored: list[tuple[int, float, str]] = []
    for index, sentence in enumerate(sentences):
        if re.search(r"\b(bukan fokus|tidak relevan|tidak terkait|tanpa bukti)\b", sentence.casefold()):
            continue
        sentence_terms = _terms(sentence)
        if not sentence_terms:
            continue
        score = 0.0
        score += len(sentence_terms.intersection(important_terms)) * 0.20
        if company_terms and any(term in sentence.casefold() for term in company_terms):
            score += 0.80
        if plan.tickers and any(re.search(rf"\b{re.escape(ticker)}\b", sentence) for ticker in plan.tickers):
            score += 0.80
        if re.search(r"(?:rp\s*)?\d+(?:[.,]\d+)?%?|20\d{2}", sentence.casefold()):
            score += 0.35
        if sentence_terms.intersection(intent_terms):
            score += 0.45
        if plan.retrieval_intents and not (
            sentence_terms.intersection(intent_terms)
            or sentence_terms.intersection(non_company_query_terms)
        ):
            score -= 0.65
        if len(sentence) < 35:
            score -= 0.25
        scored.append((index, score, sentence))

    selected = [
        item
        for item in sorted(scored, key=lambda row: row[1], reverse=True)
        if item[1] > 0
    ][:max_sentences]
    if not selected:
        return " ".join(sentences[:max_sentences])

    return " ".join(sentence for _index, _score, sentence in sorted(selected))


def _rank_key(item: RetrievedContext) -> tuple[float, float, float]:
    rerank = item.rerank_score if item.rerank_score is not None else item.score
    return (rerank, item.score, item.graph_score + item.vector_score)


def _debug_item(item: RetrievedContext) -> dict[str, Any]:
    text = _truncate(item.text, 700)
    return {
        "id": _context_debug_id(item),
        "source_id": item.source_id or item.source.get("source_id", ""),
        "source_type": item.source_type,
        "document_type": item.source_type,
        "retrieval_source": item.retrieval_source,
        "source": _source_label(item),
        "ticker": _item_ticker(item),
        "year": _item_year(item),
        "score": item.score,
        "original_score": item.initial_score,
        "semantic_score": item.semantic_score,
        "graph_score": item.graph_score,
        "vector_score": item.vector_score,
        "provenance_score": item.provenance_score,
        "exact_match_score": item.exact_match_score,
        "entity_score": item.entity_score,
        "ticker_score": item.ticker_score,
        "topic_score": item.topic_score,
        "intent_score": item.intent_score,
        "year_score": item.year_score,
        "financial_keyword_score": item.financial_keyword_score,
        "recency_score": item.recency_score,
        "rerank_score": item.rerank_score,
        "final_score": item.score,
        "accepted": item.accepted,
        "reason": item.rejection_reason or "accepted",
        "node_id": item.node_id,
        "edge_path": item.edge_path,
        "source_document": item.source.get("title")
        or item.source.get("url")
        or item.source.get("source_name")
        or "",
        "text": text,
        "context_preview": text,
        "characters": len(text),
    }


def _merge_debug_item(index: int, item: RetrievedContext) -> dict[str, Any]:
    return {
        "index": index,
        "id": _context_debug_id(item),
        "source": _source_label(item),
        "score": item.score,
        "ticker": _item_ticker(item),
        "type": item.source_type,
    }


def _rerank_debug_item(index: int, item: RetrievedContext) -> dict[str, Any]:
    return {
        "context": index,
        "id": _context_debug_id(item),
        "original_score": item.initial_score,
        "semantic": item.semantic_score,
        "graph": item.graph_score,
        "provenance": item.provenance_score,
        "entity_score": item.entity_score,
        "ticker_score": item.ticker_score,
        "topic_score": item.topic_score,
        "intent_score": item.intent_score,
        "year_score": item.year_score,
        "reranker_score": item.rerank_score,
        "final_score": item.score,
        "rank": f"#{index}",
        "source": _source_label(item),
        "ticker": _item_ticker(item),
        "type": item.source_type,
    }


def _context_debug_id(item: RetrievedContext) -> str:
    return (
        item.source_id
        or str(item.source.get("source_id") or "")
        or str(item.source.get("document_id") or "")
        or str(item.source.get("url") or "")
        or item.node_id
        or item.edge_path
        or item.key()
    )


def _source_label(item: RetrievedContext) -> str:
    return str(
        item.source.get("title")
        or item.source.get("source_name")
        or item.source.get("url")
        or item.source_id
        or item.node_id
        or item.edge_path
        or ""
    )


def _item_ticker(item: RetrievedContext) -> str:
    explicit = str(item.source.get("ticker") or item.source.get("stock_code") or "")
    if explicit:
        return explicit.upper()
    match = re.search(r"\b[A-Z]{4}\b", " ".join([item.text, _source_label(item)]))
    return match.group(0) if match else ""


def _item_year(item: RetrievedContext) -> str:
    for key in ("year", "document_year", "reporting_period", "publication_date", "published_at", "date"):
        value = item.source.get(key)
        if value:
            match = re.search(r"(20\d{2})", str(value))
            return match.group(1) if match else str(value)
    match = re.search(r"(20\d{2})", item.text)
    return match.group(1) if match else ""


def _question_year(question: str) -> int | None:
    match = re.search(r"\b(20\d{2})\b", question)
    return int(match.group(1)) if match else None


def _entity_terms(text: str) -> set[str]:
    entities = set(re.findall(r"\b[A-Z]{4}\b", text))
    lowered = text.casefold()
    for ticker, aliases in TICKER_ALIASES.items():
        if ticker.casefold() in lowered or any(alias in lowered for alias in aliases):
            entities.add(ticker)
            entities.update(aliases)
    for key in ("bank", "telkom", "astra", "indofood", "antam", "adaro", "bukit asam"):
        if key in lowered:
            entities.add(key)
    return entities


def _concept_terms(text: str, plan: QueryPlan) -> set[str]:
    terms = _terms(text)
    concepts = set(terms.intersection(FINANCIAL_KEYWORDS))
    concepts.update(terms.intersection(NEWS_KEYWORDS))
    for intent in plan.retrieval_intents:
        concepts.update(terms.intersection(INTENT_KEYWORDS.get(intent, set())))
    return concepts


def _normalize_entity(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
    for ticker, aliases in TICKER_ALIASES.items():
        if normalized == ticker.casefold() or normalized in aliases:
            return ticker.casefold()
    return normalized


def _dedupe_reason(key_type: str) -> str:
    return {
        "url": "duplicate URL",
        "article_fingerprint": "duplicate fingerprint",
        "entity": "duplicate entity",
        "financial_report": "duplicate financial report",
        "text_similarity": "95% similar",
        "normalized_text": "duplicate normalized text",
        "title_similarity": "duplicate title similarity",
        "cosine_similarity": "duplicate cosine similarity",
        "entity_overlap": "duplicate entity overlap",
    }.get(key_type, key_type)


def _classify_intent(question: str) -> Literal["simple", "financial", "news", "complex"]:
    terms = _terms(question)
    financial_hits = len(terms.intersection(FINANCIAL_KEYWORDS))
    news_hits = len(terms.intersection(NEWS_KEYWORDS))
    complex_hits = len(terms.intersection(COMPLEX_KEYWORDS))
    multi_topic = " dan " in question.casefold() or " serta " in question.casefold()
    if complex_hits >= 1 and (financial_hits or news_hits or multi_topic):
        return "complex"
    if financial_hits >= 1 and news_hits >= 1:
        return "complex"
    if news_hits >= 1:
        return "news"
    if financial_hits >= 1:
        return "financial"
    return "complex" if complex_hits >= 1 else "simple"


def _classify_retrieval_intents(question: str) -> list[str]:
    terms = _intent_terms(question)
    detected: list[tuple[str, float]] = []
    for intent, keywords in INTENT_KEYWORDS.items():
        if not keywords:
            continue
        overlap = terms.intersection(keywords)
        if not overlap:
            continue
        score = len(overlap) / max(1, min(8, len(keywords)))
        detected.append((intent, score))

    detected.sort(key=lambda item: item[1], reverse=True)
    if detected:
        return [intent for intent, _score in detected[:3]]

    if terms.intersection(FINANCIAL_KEYWORDS):
        return ["financial_performance"]
    if terms.intersection(NEWS_KEYWORDS):
        return ["news"]
    return []


def _intent_query_phrase(intent: str) -> str:
    return {
        "financial_performance": "financial performance revenue earnings",
        "risk": "risk competition regulation uncertainty debt",
        "profitability": "profitability ROE ROA margin EPS",
        "growth": "revenue growth earnings growth expansion",
        "dividend": "dividend payout ratio cash flow policy",
        "cash_flow": "cash flow operating cash capex",
        "balance_sheet": "balance sheet assets liabilities equity debt",
        "corporate_action": "corporate action acquisition merger buyback rights issue",
        "regulation": "regulation government policy impact",
        "industry_competition": "industry competition market share sector",
        "news": "latest news sentiment corporate event",
        "investment": "investment outlook buy hold risk",
        "valuation": "valuation PER PBV price",
    }.get(intent, "")


def _extract_ticker(question: str) -> str:
    tickers = _extract_tickers(question)
    return tickers[0] if tickers else ""


def _extract_tickers(question: str) -> list[str]:
    bracket_match = re.search(r"\[([A-Z]{4})\]", question)
    bracket_tickers = [bracket_match.group(1)] if bracket_match else []
    upper_tokens = re.findall(r"\b[A-Z]{4}\b", question)
    ordered = _unique([*bracket_tickers, *upper_tokens])
    matched = [token for token in ordered if token in TICKER_ALIASES]
    if matched:
        return matched
    for token in upper_tokens:
        if token in TICKER_ALIASES:
            return [token]
    return ordered


def _company_terms(ticker: str) -> list[str]:
    if not ticker:
        return []
    return _unique([ticker, *TICKER_ALIASES.get(ticker, [])])


def _subject_phrase(question: str) -> str:
    terms = [term for term in _terms(question) if term not in FINANCIAL_KEYWORDS and term not in NEWS_KEYWORDS]
    return " ".join(terms[:4])


def _context_blob(item: RetrievedContext) -> str:
    fields = [
        item.text,
        item.source_id,
        item.node_id,
        item.edge_path,
        str(item.source.get("title") or ""),
        str(item.source.get("source_name") or ""),
        str(item.source.get("ticker") or ""),
        str(item.source.get("company") or ""),
        str(item.source.get("url") or ""),
    ]
    return " ".join(fields)


def _graph_relevance(item: RetrievedContext) -> float:
    if item.retrieval_source == "graph":
        return 1.0
    if item.source_type == "graph_path" or item.edge_path:
        return 0.86
    if item.retrieval_source == "provenance":
        return 0.62
    return 0.38 if item.retrieval_source == "vector" else 0.2


def _provenance_confidence(item: RetrievedContext) -> float:
    source = item.source
    if item.source_type == "financial_report":
        base = 0.95
    elif item.source_type == "graph_path":
        base = 0.78
    elif item.source_type == "news":
        base = 0.66
    else:
        base = 0.48
    if source.get("url") or source.get("source_id") or item.source_id:
        base += 0.08
    if source.get("publication_date") or source.get("reporting_period"):
        base += 0.05
    return _normalize(base)


def _exact_match(plan: QueryPlan, text: str) -> float:
    if not plan.company_terms:
        return 0.0
    haystack = text.casefold()
    hits = 0
    for term in plan.company_terms:
        if term.casefold() in haystack:
            hits += 1
    return _normalize(hits / max(1, min(3, len(plan.company_terms))))


def _financial_keyword_match(query_text: str, text: str) -> float:
    query_terms = _terms(query_text).intersection(FINANCIAL_KEYWORDS) or FINANCIAL_KEYWORDS
    text_terms = _terms(text)
    if not text_terms:
        return 0.0
    overlap = len(query_terms.intersection(text_terms))
    return _normalize(overlap / max(1, min(8, len(query_terms))))


def _recency_bonus(item: RetrievedContext) -> float:
    raw_date = (
        item.source.get("publication_date")
        or item.source.get("published_at")
        or item.source.get("date")
        or item.source.get("reporting_period")
        or ""
    )
    parsed = _parse_date(str(raw_date))
    if parsed is None:
        return 0.15 if item.source_type == "financial_report" else 0.0
    age_days = max(0, (datetime.now(timezone.utc) - parsed).days)
    if age_days <= 45:
        return 1.0
    if age_days <= 180:
        return 0.78
    if age_days <= 365:
        return 0.55
    if age_days <= 730:
        return 0.30
    return 0.10


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    try:
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    year_match = re.search(r"(20\d{2})", normalized)
    if year_match:
        return datetime(int(year_match.group(1)), 12, 31, tzinfo=timezone.utc)
    return None


def _dedupe_key(item: RetrievedContext) -> tuple[str, str]:
    url = _canonical_url(str(item.source.get("url") or ""))
    if url:
        return ("url", f"url:{url}")
    if item.source_type == "financial_report":
        report_key = "|".join(
            str(part or "").strip().casefold()
            for part in (
                item.source.get("ticker"),
                item.source.get("source_file"),
                item.source.get("reporting_period"),
                item.source.get("page_number"),
                item.source_id,
            )
        )
        if report_key.strip("|"):
            return ("financial_report", f"report:{report_key}")
    if item.source_type == "news":
        article_key = "|".join(
            str(part or "").strip().casefold()
            for part in (
                item.source.get("title"),
                item.source.get("source_name"),
                item.source.get("publication_date"),
                _truncate(item.text, 240),
            )
        )
        return ("article_fingerprint", f"article:{hashlib.sha1(article_key.encode('utf-8')).hexdigest()}")
    if item.node_id or item.edge_path:
        return ("entity", f"entity:{item.node_id or item.edge_path}".casefold())
    return ("text_similarity", item.key())


def _canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _similar_context(
    item: RetrievedContext,
    representatives: list[RetrievedContext],
) -> tuple[RetrievedContext | None, str]:
    normalized = _normalize_text(item.text)
    for other in representatives:
        other_normalized = _normalize_text(other.text)
        if not normalized or not other_normalized:
            continue
        if normalized == other_normalized:
            return other, "normalized_text"
        if _title_similarity(item, other) > 0.92:
            return other, "title_similarity"
        if SequenceMatcher(None, normalized[:1200], other_normalized[:1200]).ratio() > 0.95:
            return other, "text_similarity"
        if _text_cosine_similarity(normalized, other_normalized) > 0.90:
            return other, "cosine_similarity"
        if _entity_overlap(item, other) > 0.85:
            return other, "entity_overlap"
    return None, ""


def _title_similarity(left: RetrievedContext, right: RetrievedContext) -> float:
    left_title = _normalize_text(str(left.source.get("title") or _source_label(left)))
    right_title = _normalize_text(str(right.source.get("title") or _source_label(right)))
    if not left_title or not right_title:
        return 0.0
    return SequenceMatcher(None, left_title, right_title).ratio()


def _text_cosine_similarity(left: str, right: str) -> float:
    left_terms = _term_counts(left)
    right_terms = _term_counts(right)
    if not left_terms or not right_terms:
        return 0.0
    common = set(left_terms).intersection(right_terms)
    dot = sum(left_terms[term] * right_terms[term] for term in common)
    left_norm = sum(value * value for value in left_terms.values()) ** 0.5
    right_norm = sum(value * value for value in right_terms.values()) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return _normalize(dot / (left_norm * right_norm))


def _entity_overlap(left: RetrievedContext, right: RetrievedContext) -> float:
    left_entities = {_normalize_entity(entity) for entity in _entity_terms(_context_blob(left))}
    right_entities = {_normalize_entity(entity) for entity in _entity_terms(_context_blob(right))}
    if len(left_entities) < 2 or len(right_entities) < 2:
        return 0.0
    overlap = left_entities.intersection(right_entities)
    return _normalize(len(overlap) / max(1, min(len(left_entities), len(right_entities))))


def _factual_density(text: str) -> float:
    if not text:
        return 0.0
    tokens = _terms(text)
    if not tokens:
        return 0.0
    number_hits = len(re.findall(r"(?:rp\s*)?\d+(?:[.,]\d+)?%?|20\d{2}", text.casefold()))
    entity_hits = len(re.findall(r"\b[A-Z]{4}\b", text))
    return _normalize((number_hits * 0.10) + (entity_hits * 0.08) + min(0.55, len(tokens) / 120))


def _known_section_types() -> set[str]:
    return {"financial_report", "structured_financial", "news", "graph_path"}


def _dedupe_facts(text: str, seen: set[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(str(text or "").split()))
    kept: list[str] = []
    for sentence in sentences:
        fingerprint = _fact_fingerprint(sentence)
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        kept.append(sentence)
    return " ".join(kept).strip()


def _fact_fingerprint(sentence: str) -> str:
    normalized = _normalize_text(sentence)
    if len(normalized) < 30:
        return ""
    return hashlib.sha1(normalized[:220].encode("utf-8")).hexdigest()


def _terms(text: str) -> set[str]:
    stopwords = {
        "apa",
        "yang",
        "dan",
        "atau",
        "di",
        "ke",
        "dari",
        "ini",
        "itu",
        "untuk",
        "dengan",
        "pada",
        "saham",
        "emiten",
        "bagaimana",
        "berdasarkan",
        "apakah",
        "kalau",
        "mau",
        "beli",
        "oke",
        "gak",
        "atau",
        "jangan",
        "dulu",
    }
    return {
        term
        for term in re.findall(r"[a-zA-Z0-9]{3,}", text.casefold())
        if term not in stopwords
    }


def _intent_terms(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9]{3,}", text.casefold()))


def _term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for term in _terms(text):
        counts[term] = counts.get(term, 0) + 1
    return counts


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9%.,:/-]+", " ", str(text).casefold())).strip()


def _truncate(text: str, max_chars: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rsplit(" ", 1)[0].strip()


def _sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", str(text or "").strip())
        if part.strip()
    ]


def _normalize(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


__all__ = [
    "QueryPlan",
    "RetrievalConfig",
    "RetrievedContext",
    "deduplicate_contexts",
    "dynamic_top_k",
    "make_vector_contexts",
    "optimize_contexts",
    "prompt_context_block",
    "rewrite_queries",
    "semantic_retrieval_prompt",
    "source_list_from_contexts",
]
