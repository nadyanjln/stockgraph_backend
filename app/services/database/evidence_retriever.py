"""Deterministic hybrid retrieval from StockGraph's provenance registry.

This is the exact-entity and graph-traversal layer that complements semantic
GraphRAG retrieval. It remains available when a year-specific vector query is
empty and gives the agents structured, provenance-safe evidence.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from app.services.database.graph_builder import PROVENANCE_PATH, list_year_graphs
from app.services.database.retrieval_optimizer import (
    RetrievalConfig,
    RetrievedContext,
)

EvidenceKind = Literal["news", "financial_report"]

COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "BBCA": ("bbca", "bank central asia", "bca"),
    "BBRI": ("bbri", "bank rakyat indonesia", "bri"),
    "BMRI": ("bmri", "bank mandiri"),
    "BBNI": ("bbni", "bank negara indonesia", "bni"),
    "TLKM": ("tlkm", "telkom indonesia", "telkom"),
}
KNOWN_NEWS_DOMAINS = (
    "bisnis.com",
    "cnbcindonesia.com",
    "kontan.co.id",
    "antaranews.com",
    "bloombergtechnoz.com",
    "idxchannel.com",
)


@dataclass(slots=True)
class RetrievalDiagnostics:
    query: str
    ticker_detected: str = ""
    entity_resolved: bool = False
    news_found: int = 0
    news_after_relevance_filter: int = 0
    financial_reports_found: int = 0
    financial_chunks_found: int = 0
    graph_nodes_found: int = 0
    graph_edges_traversed: int = 0
    graph_contexts_retrieved: int = 0
    vector_chunks_retrieved: int = 0
    merged_context_count: int = 0
    reranked_context_count: int = 0
    final_context_count: int = 0
    retrieval_strategy_used: list[str] = field(default_factory=list)
    fallback_reason: str | None = None
    financial_period_used: str = ""
    corpus_status: str = "unknown"
    graph_ingestion_status: str = "unknown"
    retrieval_status: str = "unknown"

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "ticker_detected": self.ticker_detected,
            "entity_resolved": self.entity_resolved,
            "news_found": self.news_found,
            "news_after_relevance_filter": self.news_after_relevance_filter,
            "financial_reports_found": self.financial_reports_found,
            "financial_chunks_found": self.financial_chunks_found,
            "graph_nodes_found": self.graph_nodes_found,
            "graph_edges_traversed": self.graph_edges_traversed,
            "graph_contexts_retrieved": self.graph_contexts_retrieved,
            "vector_chunks_retrieved": self.vector_chunks_retrieved,
            "merged_context_count": self.merged_context_count,
            "reranked_context_count": self.reranked_context_count,
            "final_context_count": self.final_context_count,
            "retrieval_strategy_used": self.retrieval_strategy_used,
            "fallback_reason": self.fallback_reason,
            "financial_period_used": self.financial_period_used,
            "corpus_status": self.corpus_status,
            "graph_ingestion_status": self.graph_ingestion_status,
            "retrieval_status": self.retrieval_status,
        }


@dataclass(slots=True)
class EvidenceBundle:
    ticker: str
    news_snippets: list[str] = field(default_factory=list)
    financial_snippets: list[str] = field(default_factory=list)
    graph_paths: list[str] = field(default_factory=list)
    news_sources: list[dict[str, str]] = field(default_factory=list)
    financial_sources: list[dict[str, str]] = field(default_factory=list)
    contexts: list[RetrievedContext] = field(default_factory=list)
    diagnostics: RetrievalDiagnostics | None = None


def _registry() -> dict:
    path = Path(os.getenv("STOCKGRAPH_PROVENANCE_REGISTRY", str(PROVENANCE_PATH)))
    if not path.exists():
        return {"articles": {}, "nodes": {}, "edges": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"articles": {}, "nodes": {}, "edges": {}}
    return {
        "articles": data.get("articles", {}),
        "nodes": data.get("nodes", {}),
        "edges": data.get("edges", {}),
    }


def resolve_ticker(question: str, registry: dict | None = None) -> str:
    text = question.lower()
    for ticker, aliases in COMPANY_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases):
            return ticker

    data = registry or _registry()
    known = {
        str(node.get("label", "")).upper()
        for node in data["nodes"].values()
        if node.get("type") == "stock"
    }
    for match in re.findall(r"\b[A-Z]{4}\b", question.upper()):
        if match in known:
            return match
    return ""


def _year_from_label(label: str) -> int:
    match = re.search(r"\b(20\d{2})\b", label)
    return int(match.group(1)) if match else 0


def _date_key(value: str) -> datetime:
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
    return datetime.min


def _normalized_publisher(value: str, url: str) -> str:
    host = re.sub(r"^www\.", "", urlparse(url).hostname or "")
    for domain in KNOWN_NEWS_DOMAINS:
        if host == domain or host.endswith(f".{domain}"):
            return domain
    return value


def _traverse(registry: dict, seed: str, max_hops: int) -> tuple[set[str], list[dict]]:
    selected = {seed}
    frontier = {seed}
    traversed: list[dict] = []
    for _ in range(max(0, min(max_hops, 3))):
        next_frontier: set[str] = set()
        for edge in registry["edges"].values():
            if edge.get("source") in frontier or edge.get("target") in frontier:
                traversed.append(edge)
                next_frontier.update((edge.get("source", ""), edge.get("target", "")))
        next_frontier.discard("")
        next_frontier -= selected
        selected.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
    return selected, traversed


def retrieve_local_evidence(
    question: str,
    target_year: int | None = None,
    max_hops: int = 2,
    news_limit: int = 6,
    config: RetrievalConfig | None = None,
) -> EvidenceBundle:
    cfg = config or RetrievalConfig.from_env()
    registry = _registry()
    diagnostics = RetrievalDiagnostics(query=question)
    ticker = resolve_ticker(question, registry)
    diagnostics.ticker_detected = ticker
    if not ticker:
        diagnostics.corpus_status = "entity_unresolved"
        diagnostics.retrieval_status = "not_started"
        diagnostics.fallback_reason = "ticker_not_resolved"
        return EvidenceBundle(ticker="", diagnostics=diagnostics)

    stock_id = f"stock:{ticker}"
    diagnostics.entity_resolved = stock_id in registry["nodes"]
    diagnostics.retrieval_strategy_used.append("exact_ticker")
    if not diagnostics.entity_resolved:
        diagnostics.corpus_status = "ticker_missing_from_provenance"
        diagnostics.retrieval_status = "exact_entity_failed"
        diagnostics.fallback_reason = "ticker_node_not_found"
        return EvidenceBundle(ticker=ticker, diagnostics=diagnostics)

    effective_hops = max_hops if max_hops is not None else cfg.graph_depth
    node_ids, traversed_edges = _traverse(registry, stock_id, effective_hops)
    diagnostics.retrieval_strategy_used.append(f"graph_traversal_{effective_hops}_hop")
    diagnostics.graph_nodes_found = len(node_ids)
    diagnostics.graph_edges_traversed = len(
        {str(edge.get("id")) for edge in traversed_edges if edge.get("id")}
    )

    articles = [
        article
        for article in registry["articles"].values()
        if ticker in article.get("stock_codes", [])
    ]
    diagnostics.news_found = len(articles)
    articles.sort(
        key=lambda item: (_date_key(str(item.get("publication_date", ""))), item.get("title", "")),
        reverse=True,
    )
    selected_articles = articles[: min(news_limit, cfg.top_k_graph)]
    diagnostics.news_after_relevance_filter = len(selected_articles)
    if selected_articles:
        diagnostics.retrieval_strategy_used.append("provenance_news")

    news_snippets: list[str] = []
    news_sources: list[dict[str, str]] = []
    contexts: list[RetrievedContext] = []
    for article in selected_articles:
        title = str(article.get("title") or "Berita terkait")
        summary = str(article.get("summary") or "")
        published = str(article.get("publication_date") or "")
        publisher = _normalized_publisher(
            str(article.get("publisher") or ""),
            str(article.get("url") or ""),
        )
        snippet = "\n".join(
            [
                "Jenis evidence: Berita",
                f"Kode saham: {ticker}",
                f"Judul: {title}",
                f"Publisher: {publisher or '-'}",
                f"Tanggal publikasi: {published or '-'}",
                f"Ringkasan: {summary or '-'}",
            ]
        )
        news_snippets.append(snippet)
        article_source = {
            "source_id": str(article.get("id") or ""),
            "source_type": "news",
            "title": title,
            "source_name": publisher,
            "publisher": publisher,
            "url": str(article.get("url") or ""),
            "publication_date": published,
            "reporting_period": "",
            "snippet": summary[:700],
            "retrieved_text": summary[:2000],
        }
        news_sources.append(
            article_source
        )
        contexts.append(
            RetrievedContext(
                text=snippet,
                source_type="news",
                retrieval_source="provenance",
                source=article_source,
                score=0.25,
                source_id=str(article.get("id") or ""),
            )
        )

    financial_nodes = [
        node
        for node in registry["nodes"].values()
        if node.get("type") == "financial"
        and ticker in node.get("stock_codes", [])
    ]
    years = sorted({_year_from_label(str(node.get("label", ""))) for node in financial_nodes} - {0})
    available_years = [year for year in years if not target_year or year <= target_year]
    latest_year = max(available_years or years, default=0)
    selected_financial = [
        node
        for node in financial_nodes
        if _year_from_label(str(node.get("label", ""))) == latest_year
    ]
    diagnostics.financial_reports_found = len(years)
    diagnostics.financial_chunks_found = len(selected_financial)
    if latest_year:
        diagnostics.financial_period_used = f"FY {latest_year}"
        diagnostics.retrieval_strategy_used.append("latest_available_financial_period")

    financial_snippets = [
        "\n".join(
            [
                "Jenis evidence: Laporan Keuangan IDX",
                f"Kode saham: {ticker}",
                f"Periode laporan: FY {latest_year}",
                f"Metrik: {node.get('label')}",
                f"Nilai: {node.get('description') or '-'}",
            ]
        )
        for node in selected_financial
    ]
    financial_sources: list[dict[str, str]] = []
    if selected_financial:
        combined = "; ".join(
            f"{node.get('label')}: {node.get('description')}"
            for node in selected_financial
        )
        financial_source = {
            "source_id": f"financial:{ticker}:{latest_year}",
            "source_type": "financial_report",
            "title": f"Laporan Keuangan {ticker}",
            "source_name": ticker,
            "publisher": "",
            "url": "",
            "publication_date": "",
            "reporting_period": f"FY {latest_year}",
            "snippet": combined[:700],
            "retrieved_text": combined[:2000],
        }
        financial_sources.append(financial_source)
        for snippet in financial_snippets:
            contexts.append(
                RetrievedContext(
                    text=snippet,
                    source_type="financial_report",
                    retrieval_source="graph",
                    source=financial_source,
                    score=0.35,
                    graph_score=0.35,
                    source_id=financial_source["source_id"],
                )
            )

    graph_paths: list[str] = []
    nodes = registry["nodes"]
    seen_paths: set[str] = set()
    for edge in traversed_edges:
        source = nodes.get(edge.get("source"), {})
        target = nodes.get(edge.get("target"), {})
        path = (
            f"{source.get('label', edge.get('source'))} "
            f"--[{edge.get('type', 'RELATED_TO')}]--> "
            f"{target.get('label', edge.get('target'))}"
        )
        if path not in seen_paths:
            seen_paths.add(path)
            graph_paths.append(path)
            contexts.append(
                RetrievedContext(
                    text=(
                        "Jenis evidence: Relasi Knowledge Graph\n"
                        f"Kode saham: {ticker}\n"
                        f"Path: {path}"
                    ),
                    source_type="graph_path",
                    retrieval_source="graph",
                    score=0.2,
                    graph_score=0.2,
                    edge_path=path,
                    node_id=str(edge.get("source") or ""),
                )
            )
        if len(graph_paths) >= 12:
            break
    diagnostics.graph_contexts_retrieved = len(graph_paths)

    if not news_snippets and not financial_snippets:
        diagnostics.fallback_reason = "no_valid_evidence_in_provenance_corpus"
        diagnostics.corpus_status = "no_valid_evidence"
        diagnostics.retrieval_status = "all_local_strategies_exhausted"
    else:
        diagnostics.corpus_status = "evidence_available"
        diagnostics.retrieval_status = "provenance_retrieval_success"

    evidence_years = {
        int(article.get("year") or 0)
        for article in selected_articles
        if int(article.get("year") or 0) > 0
    }
    if latest_year:
        evidence_years.add(latest_year)
    physical_years = set(list_year_graphs())
    ingested_years = evidence_years.intersection(physical_years)
    if evidence_years and ingested_years == evidence_years:
        diagnostics.graph_ingestion_status = "all_evidence_years_ingested"
    elif ingested_years:
        diagnostics.graph_ingestion_status = "partially_ingested"
    elif evidence_years:
        diagnostics.graph_ingestion_status = "provenance_available_graph_empty"
    else:
        diagnostics.graph_ingestion_status = "no_evidence_year"

    return EvidenceBundle(
        ticker=ticker,
        news_snippets=news_snippets,
        financial_snippets=financial_snippets,
        graph_paths=graph_paths,
        news_sources=news_sources,
        financial_sources=financial_sources,
        contexts=contexts,
        diagnostics=diagnostics,
    )


__all__ = [
    "EvidenceBundle",
    "RetrievalDiagnostics",
    "resolve_ticker",
    "retrieve_local_evidence",
]
