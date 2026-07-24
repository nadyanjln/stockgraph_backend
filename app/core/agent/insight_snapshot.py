"""Build stable conversation insight snapshots from validated evidence."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.core.extractor.entity_validation import is_valid_entity, normalized_entity_key
from app.core.extractor.relevance_checker import CheckedResult
from app.services.crawler.financial_fetcher import FundamentalData
from app.services.crawler.news_crawler import Article

_POSITIVE = {
    "naik", "meningkat", "tumbuh", "pertumbuhan", "menguat", "laba", "surplus",
    "ekspansi", "positif", "rekor", "membaik", "optimis",
}
_NEGATIVE = {
    "turun", "menurun", "melemah", "rugi", "kerugian", "tekanan", "risiko",
    "anjlok", "negatif", "gagal", "utang", "penurunan",
}


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _article_id(url: str) -> str:
    return f"article:{hashlib.sha1(url.encode('utf-8')).hexdigest()[:14]}"


def _sentiment(articles: list[Article]) -> tuple[str, float | None, str]:
    if len(articles) < 2:
        return (
            "insufficient_data",
            None,
            "Belum cukup berita relevan untuk menyimpulkan sentimen.",
        )
    article_scores: list[int] = []
    for article in articles:
        words = set(
            "".join(
                char.lower() if char.isalnum() else " "
                for char in f"{article.title} {article.text}"
            ).split()
        )
        article_scores.append(
            sum(word in words for word in _POSITIVE)
            - sum(word in words for word in _NEGATIVE)
        )
    positive = sum(score > 0 for score in article_scores)
    negative = sum(score < 0 for score in article_scores)
    score = sum(article_scores) / max(1, len(article_scores))
    if positive and negative:
        label = "mixed"
    elif score > 0:
        label = "positive"
    elif score < 0:
        label = "negative"
    else:
        label = "neutral"
    return (
        label,
        round(score, 3),
        f"Berdasarkan {len(articles)} berita relevan pada snapshot sumber ini.",
    )


def _graph_counts_from_provenance(tickers: list[str], graph_stats: dict) -> tuple[int, int]:
    fallback_nodes = sum(int(getattr(stat, "nodes_created", 0) or 0) for stat in graph_stats.values())
    fallback_edges = sum(int(getattr(stat, "edges_created", 0) or 0) for stat in graph_stats.values())
    try:
        from app.services.database.graph_builder import explore_provenance_graph

        graph = explore_provenance_graph(stock_codes=tickers, limit=300)
        analytics = graph.get("analytics", {})
        node_count = int(analytics.get("node_count") or 0)
        edge_count = int(analytics.get("relationship_count") or 0)
        return (node_count or fallback_nodes, edge_count or fallback_edges)
    except Exception:
        return fallback_nodes, fallback_edges


def build_conversation_insight_snapshot(
    checked: dict[str, list[CheckedResult]],
    articles: dict[str, list[Article]],
    financial: dict[str, FundamentalData],
    graph_stats: dict,
) -> dict:
    tickers = sorted({code.upper() for code in set(checked) | set(articles) | set(financial)})
    ticker = tickers[0] if len(tickers) == 1 else ",".join(tickers)
    article_by_url = {
        article.url: article
        for article_list in articles.values()
        for article in article_list
    }
    passed_news = [
        item
        for items in checked.values()
        for item in items
        if item.score.passed
        and item.extraction.source_type == "news"
        and item.extraction.source_ref in article_by_url
    ]
    valid_articles = [
        article_by_url[item.extraction.source_ref]
        for item in passed_news
    ]
    news_source_ids = sorted({_article_id(article.url) for article in valid_articles})
    financial_source_ids = sorted(
        {
            f"idx:{code.upper()}:{snapshot.year}"
            for code, data in financial.items()
            for snapshot in data.historical
            if snapshot.year > 0
        }
    )
    source_ids = [*news_source_ids, *financial_source_ids]

    entities: dict[str, dict] = {}
    for item in passed_news:
        source_id = _article_id(item.extraction.source_ref)
        for entity in item.extraction.entities:
            if not is_valid_entity(entity, has_evidence=True):
                continue
            key = normalized_entity_key(entity)
            current = entities.setdefault(
                key,
                {
                    "id": entity.id,
                    "label": entity.name.strip(),
                    "type": entity.type.lower(),
                    "source_ids": [],
                },
            )
            current["source_ids"] = sorted(set([*current["source_ids"], source_id]))

    if ticker:
        entities.setdefault(
            ticker.casefold(),
            {
                "id": ticker.casefold(),
                "label": ticker,
                "type": "stock",
                "source_ids": news_source_ids or financial_source_ids,
            },
        )

    sentiment, sentiment_score, sentiment_reason = _sentiment(valid_articles)
    dates = sorted(article.published for article in valid_articles if article.published)
    now = datetime.now(UTC).isoformat()
    graph_node_count, graph_relation_count = _graph_counts_from_provenance(tickers, graph_stats)
    snapshot_basis = "|".join([
        ticker,
        *source_ids,
        f"nodes:{graph_node_count}",
        f"relations:{graph_relation_count}",
    ])
    company_name = next(
        (data.company_name for data in financial.values() if data.company_name),
        "",
    )
    return {
        "conversation_id": "",
        "ticker": ticker,
        "company_name": company_name,
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "sentiment_reason": sentiment_reason,
        "source_snapshot_id": _stable_id(ticker.lower() or "stock", snapshot_basis),
        "source_count": len(source_ids),
        "news_source_count": len(news_source_ids),
        "financial_report_count": len(financial_source_ids),
        "source_ids": source_ids,
        "entities": sorted(
            entities.values(),
            key=lambda item: (-len(item["source_ids"]), item["label"].casefold()),
        )[:12],
        "entity_ids": [item["id"] for item in entities.values()],
        "graph_node_count": graph_node_count,
        "graph_relation_count": graph_relation_count,
        "period_start": dates[0] if dates else None,
        "period_end": dates[-1] if dates else None,
        "generated_at": now,
        "updated_at": now,
    }
