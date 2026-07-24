"""
GraphRAG-SDK graph builder for StockGraph.

This module intentionally does not write Cypher. It converts crawled news and
IDX/yfinance financial data into domain documents, then delegates graph
construction to GraphRAG-SDK through ``rag.ingest`` via ``GraphRAGEngine``.

Graph naming convention: ``stockgraph_{year}``, for example ``stockgraph_2024``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from falkordb import FalkorDB

from app.core.extractor.relevance_checker import CheckedResult
from app.core.extractor.entity_validation import is_valid_entity
from app.services.crawler.financial_fetcher import FundamentalData, YearlySnapshot
from app.services.crawler.news_crawler import Article

load_dotenv()

GRAPH_PREFIX = "stockgraph"
REGISTRY_PATH = Path(os.getenv("STOCKGRAPH_REGISTRY", ".stockgraph_registry.json"))
PROVENANCE_PATH = Path(
    os.getenv("STOCKGRAPH_PROVENANCE_REGISTRY", ".stockgraph_graph.json")
)

SECTOR_BY_STOCK: dict[str, str] = {
    "BBCA": "perbankan", "BBRI": "perbankan", "BMRI": "perbankan", "BBNI": "perbankan",
    "BNGA": "perbankan", "BTPS": "perbankan", "PNBN": "perbankan", "BJTM": "perbankan",
    "TLKM": "telekomunikasi", "EXCL": "telekomunikasi", "ISAT": "telekomunikasi",
    "ASII": "otomotif", "AUTO": "otomotif",
    "UNVR": "consumer", "ICBP": "consumer", "MYOR": "consumer", "HMSP": "consumer",
    "GOTO": "teknologi", "BUKA": "teknologi",
}


@dataclass
class GraphStats:
    nodes_created: int = 0
    nodes_merged: int = 0
    edges_created: int = 0
    edges_merged: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)
    graph_name: str = ""
    documents_ingested: int = 0


@dataclass(frozen=True)
class IngestDocument:
    year: int
    document_id: str
    text: str


def graph_name_for_year(year: int) -> str:
    return f"{GRAPH_PREFIX}_{year}"


def _registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"years": []}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"years": []}


def _save_registry(data: dict) -> None:
    REGISTRY_PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _provenance_registry() -> dict:
    if not PROVENANCE_PATH.exists():
        return {"articles": {}, "nodes": {}, "edges": {}}
    try:
        data = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
        return {
            "articles": data.get("articles", {}),
            "nodes": data.get("nodes", {}),
            "edges": data.get("edges", {}),
        }
    except (json.JSONDecodeError, OSError):
        return {"articles": {}, "nodes": {}, "edges": {}}


def _save_provenance_registry(data: dict) -> None:
    PROVENANCE_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:14]
    return f"{prefix}:{digest}"


def _article_summary(text: str, limit: int = 520) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit].rsplit(' ', 1)[0]}…"


def _node_kind(entity_type: str) -> str:
    mapping = {
        "STOCK": "stock",
        "ORGANIZATION": "company",
        "PERSON": "person",
        "POLICY": "topic",
        "EVENT": "topic",
    }
    return mapping.get(entity_type.upper(), "topic")


def _upsert_node(
    registry: dict,
    node_id: str,
    label: str,
    kind: str,
    description: str = "",
    stock_codes: list[str] | None = None,
    source_ids: list[str] | None = None,
) -> None:
    nodes = registry["nodes"]
    current = nodes.get(node_id, {})
    nodes[node_id] = {
        "id": node_id,
        "label": label or current.get("label") or node_id,
        "type": kind or current.get("type") or "topic",
        "description": description or current.get("description") or "",
        "stock_codes": sorted(set(current.get("stock_codes", []) + (stock_codes or []))),
        "source_ids": sorted(set(current.get("source_ids", []) + (source_ids or []))),
    }


def _upsert_edge(
    registry: dict,
    source: str,
    target: str,
    relation: str,
    description: str = "",
    source_ids: list[str] | None = None,
) -> None:
    edge_id = _stable_id("edge", f"{source}|{relation}|{target}")
    edges = registry["edges"]
    current = edges.get(edge_id, {})
    edges[edge_id] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "type": relation,
        "description": description or current.get("description") or "",
        "source_ids": sorted(set(current.get("source_ids", []) + (source_ids or []))),
    }


def _build_provenance_snapshot(
    checked_results: dict[str, list[CheckedResult]],
    articles: dict[str, list[Article]],
    financial_data: dict[str, FundamentalData],
    only_passed: bool,
) -> dict:
    registry = _provenance_registry()
    article_by_url = {
        article.url: article
        for article_list in articles.values()
        for article in article_list
    }

    for code, items in checked_results.items():
        stock_id = f"stock:{code.upper()}"
        _upsert_node(registry, stock_id, code.upper(), "stock", stock_codes=[code.upper()])

        for checked in items:
            if only_passed and not checked.score.passed:
                continue
            extraction = checked.extraction
            article = article_by_url.get(extraction.source_ref)
            source_ids: list[str] = []

            if extraction.source_type == "news" and article is not None:
                article_id = _stable_id("article", article.url)
                source_ids = [article_id]
                year = _publication_year(article.published)
                registry["articles"][article_id] = {
                    "id": article_id,
                    "title": article.title,
                    "publisher": article.source,
                    "publication_date": article.published,
                    "url": article.url,
                    "summary": _article_summary(article.text),
                    "stock_codes": [article.stock_code],
                    "entity_ids": [
                        entity.id
                        for entity in extraction.entities
                        if is_valid_entity(entity, has_evidence=True)
                    ],
                    "relationship_ids": [],
                    "year": year,
                }
                _upsert_node(
                    registry,
                    article_id,
                    article.title,
                    "article",
                    _article_summary(article.text, 240),
                    [article.stock_code],
                    [article_id],
                )
                year_id = f"year:{year}"
                _upsert_node(registry, year_id, str(year), "year", stock_codes=[code.upper()])
                _upsert_edge(registry, article_id, stock_id, "COVERS", source_ids=[article_id])
                _upsert_edge(registry, article_id, year_id, "PUBLISHED_IN", source_ids=[article_id])

            entity_ids: dict[str, str] = {}
            for entity in extraction.entities:
                if not is_valid_entity(entity, has_evidence=bool(source_ids)):
                    continue
                node_id = (
                    f"stock:{entity.name.upper()}"
                    if entity.type == "STOCK" and len(entity.name.strip()) <= 6
                    else f"entity:{entity.id.lower()}"
                )
                entity_ids[entity.id] = node_id
                description = str(
                    entity.attributes.get("keterangan")
                    or entity.attributes.get("jabatan")
                    or ""
                )
                _upsert_node(
                    registry,
                    node_id,
                    entity.name,
                    _node_kind(entity.type),
                    description,
                    [code.upper()],
                    source_ids,
                )
                if article is not None:
                    _upsert_edge(
                        registry,
                        _stable_id("article", article.url),
                        node_id,
                        "MENTIONS",
                        source_ids=source_ids,
                    )

            for relation in extraction.relations:
                source_node = entity_ids.get(relation.source)
                target_node = entity_ids.get(relation.target)
                if not source_node or not target_node:
                    continue
                _upsert_edge(
                    registry,
                    source_node,
                    target_node,
                    relation.type,
                    relation.description,
                    source_ids,
                )

    for code, data in financial_data.items():
        stock_id = f"stock:{code.upper()}"
        _upsert_node(
            registry,
            stock_id,
            code.upper(),
            "stock",
            data.company_name or "",
            [code.upper()],
        )
        for snapshot in data.historical:
            if snapshot.year <= 0:
                continue
            year_id = f"year:{snapshot.year}"
            _upsert_node(registry, year_id, str(snapshot.year), "year", stock_codes=[code.upper()])
            _upsert_edge(registry, stock_id, year_id, "HAS_PERIOD")
            metrics = {
                "Revenue": snapshot.revenue,
                "Net Profit": snapshot.net_profit,
                "EPS": snapshot.eps,
                "Total Assets": snapshot.total_assets,
                "Total Equity": snapshot.total_equity,
            }
            for metric_name, value in metrics.items():
                if value is None:
                    continue
                metric_id = _stable_id(
                    "metric", f"{code}:{snapshot.year}:{metric_name}"
                )
                _upsert_node(
                    registry,
                    metric_id,
                    f"{metric_name} {snapshot.year}",
                    "financial",
                    f"{metric_name}: {value}",
                    [code.upper()],
                )
                _upsert_edge(registry, stock_id, metric_id, "REPORTS_FINANCIAL")
                _upsert_edge(registry, metric_id, year_id, "FOR_PERIOD")

    article_relationships: dict[str, list[str]] = {}
    for edge_id, edge in registry["edges"].items():
        for source_id in edge.get("source_ids", []):
            article_relationships.setdefault(source_id, []).append(edge_id)
    for article_id, article in registry["articles"].items():
        article["relationship_ids"] = sorted(article_relationships.get(article_id, []))

    _save_provenance_registry(registry)
    return registry


def _register_years(years: Iterable[int]) -> None:
    data = _registry()
    known = {int(y) for y in data.get("years", []) if str(y).isdigit()}
    known.update(int(y) for y in years if int(y) > 0)
    data["years"] = sorted(known)
    _save_registry(data)


def _sync_registry_years(years: Iterable[int]) -> None:
    _save_registry({"years": sorted({int(year) for year in years if int(year) > 0})})


def list_year_graphs(host: str | None = None, port: int | None = None) -> list[int]:
    """Return physically non-empty year graphs, not registry placeholders."""
    graph_host = host or os.getenv("FALKORDB_HOST", "localhost")
    graph_port = port or int(os.getenv("FALKORDB_PORT", "6379"))
    try:
        client = FalkorDB(host=graph_host, port=graph_port)
        years: list[int] = []
        for name in client.list_graphs():
            if not name.startswith(f"{GRAPH_PREFIX}_"):
                continue
            suffix = name.removeprefix(f"{GRAPH_PREFIX}_")
            if not suffix.isdigit():
                continue
            graph = client.select_graph(name)
            count = int(graph.query("MATCH (n) RETURN count(n)").result_set[0][0])
            if count > 0:
                years.append(int(suffix))
        physical_years = sorted(years)
        _sync_registry_years(physical_years)
        return physical_years
    except Exception:
        return sorted(
            int(y) for y in _registry().get("years", []) if str(y).isdigit()
        )


def explore_provenance_graph(
    stock_codes: list[str] | None = None,
    year: int | None = None,
    node_id: str | None = None,
    depth: int = 1,
    limit: int = 120,
) -> dict:
    registry = _provenance_registry()
    codes = {code.strip().upper() for code in (stock_codes or []) if code.strip()}
    all_nodes: dict[str, dict] = registry["nodes"]
    all_edges: dict[str, dict] = registry["edges"]

    seed_ids: set[str] = set()
    if node_id and node_id in all_nodes:
        seed_ids.add(node_id)
    if codes:
        seed_ids.update(f"stock:{code}" for code in codes if f"stock:{code}" in all_nodes)
    if year is not None and f"year:{year}" in all_nodes:
        seed_ids.add(f"year:{year}")
    if not seed_ids:
        seed_ids.update(
            node["id"]
            for node in all_nodes.values()
            if not codes or codes.intersection(node.get("stock_codes", []))
        )

    selected = set(seed_ids)
    frontier = set(seed_ids)
    for _ in range(max(0, min(depth, 3))):
        next_frontier: set[str] = set()
        for edge in all_edges.values():
            if edge["source"] in frontier:
                next_frontier.add(edge["target"])
            if edge["target"] in frontier:
                next_frontier.add(edge["source"])
        next_frontier -= selected
        selected.update(next_frontier)
        frontier = next_frontier
        if not frontier or len(selected) >= limit:
            break

    if year is not None:
        year_articles = {
            article_id
            for article_id, article in registry["articles"].items()
            if int(article.get("year") or 0) == year
        }
        year_related = {
            endpoint
            for edge in all_edges.values()
            if set(edge.get("source_ids", [])).intersection(year_articles)
            for endpoint in (edge["source"], edge["target"])
        }
        selected &= year_related | year_articles | {f"year:{year}"}

    priority = {
        "stock": 0,
        "article": 1,
        "topic": 2,
        "company": 2,
        "person": 2,
        "financial": 3,
        "year": 4,
    }
    selected_nodes = sorted(
        (all_nodes[item] for item in selected if item in all_nodes),
        key=lambda item: (
            priority.get(item.get("type", ""), 9),
            item.get("label", ""),
        ),
    )[:limit]
    selected_ids = {node["id"] for node in selected_nodes}
    selected_edges = [
        edge
        for edge in all_edges.values()
        if edge["source"] in selected_ids and edge["target"] in selected_ids
    ]
    source_ids = {
        source_id
        for node in selected_nodes
        for source_id in node.get("source_ids", [])
    }
    source_ids.update(
        source_id
        for edge in selected_edges
        for source_id in edge.get("source_ids", [])
    )
    articles = [
        registry["articles"][source_id]
        for source_id in source_ids
        if source_id in registry["articles"]
    ]

    degree = {item: 0 for item in selected_ids}
    for edge in selected_edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    analytics = {
        "node_count": len(selected_nodes),
        "relationship_count": len(selected_edges),
        "article_count": len(articles),
        "most_connected": sorted(
            (
                {"id": item, "label": all_nodes[item]["label"], "count": count}
                for item, count in degree.items()
            ),
            key=lambda item: item["count"],
            reverse=True,
        )[:5],
        "most_cited": sorted(
            (
                {
                    "id": node["id"],
                    "label": node["label"],
                    "count": len(node.get("source_ids", [])),
                }
                for node in selected_nodes
                if node.get("source_ids")
            ),
            key=lambda item: item["count"],
            reverse=True,
        )[:5],
    }
    return {
        "nodes": [
            {
                **node,
                "degree": degree.get(node["id"], 0),
                "source_count": len(node.get("source_ids", [])),
            }
            for node in selected_nodes
        ],
        "edges": selected_edges,
        "articles": articles,
        "analytics": analytics,
    }


def _publication_year(published: str) -> int:
    if not published:
        return datetime.now().year
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d"):
        try:
            return datetime.strptime(published.strip(), fmt).year
        except ValueError:
            continue
    return datetime.now().year


def _article_document(article: Article) -> IngestDocument:
    year = _publication_year(article.published)
    text = "\n".join([
        "Jenis dokumen: Berita pasar modal Indonesia",
        f"Tahun: {year}",
        f"Kode saham: {article.stock_code}",
        f"Sektor: {SECTOR_BY_STOCK.get(article.stock_code, 'lainnya')}",
        f"Sumber: {article.source}",
        f"URL: {article.url}",
        f"Tanggal publikasi: {article.published}",
        f"Judul: {article.title}",
        "",
        article.text,
    ])
    return IngestDocument(year=year, document_id=article.url, text=text)


def _financial_document(code: str, data: FundamentalData, snap: YearlySnapshot) -> IngestDocument:
    lines = [
        "Jenis dokumen: Laporan keuangan emiten BEI",
        f"Tahun fiskal: {snap.year}",
        f"Kode saham: {code}",
        f"Nama perusahaan: {data.company_name or code}",
        f"Sektor: {SECTOR_BY_STOCK.get(code, 'lainnya')}",
        "Sumber: IDX financial statement dan data pasar terstruktur",
    ]
    metrics = {
        "Pendapatan": snap.revenue,
        "Pendapatan bunga bersih": snap.net_interest_income,
        "Laba bersih": snap.net_profit,
        "Total aset": snap.total_assets,
        "Total ekuitas": snap.total_equity,
        "Total utang": snap.total_debt,
        "Arus kas operasi": snap.operating_cash_flow,
        "EPS": snap.eps,
    }
    for label, value in metrics.items():
        if value is not None:
            lines.append(f"{label}: {value}")
    if snap.pdf_path:
        lines.append(f"Referensi IDX PDF: {snap.pdf_path}")
    if snap.raw_text:
        lines.extend(["", "Kutipan laporan:", snap.raw_text[:4000]])

    return IngestDocument(
        year=snap.year,
        document_id=f"idx:{code}:{snap.year}",
        text="\n".join(lines),
    )


def _documents_from_news(
    checked_results: dict[str, list[CheckedResult]],
    articles: dict[str, list[Article]],
    only_passed: bool,
) -> list[IngestDocument]:
    passed_refs: set[str] = set()
    for items in checked_results.values():
        for checked in items:
            if only_passed and not checked.score.passed:
                continue
            if checked.extraction.source_type == "news":
                passed_refs.add(checked.extraction.source_ref)

    docs: list[IngestDocument] = []
    for article_list in articles.values():
        for article in article_list:
            if not only_passed or article.url in passed_refs:
                docs.append(_article_document(article))
    return docs


def _documents_from_financial(financial_data: dict[str, FundamentalData]) -> list[IngestDocument]:
    docs: list[IngestDocument] = []
    for code, data in financial_data.items():
        for snap in data.historical:
            if snap.year > 0:
                docs.append(_financial_document(code, data, snap))
    return docs


def _merge_stats(target: GraphStats, source: GraphStats) -> None:
    target.nodes_created += source.nodes_created
    target.nodes_merged += source.nodes_merged
    target.edges_created += source.edges_created
    target.edges_merged += source.edges_merged
    target.errors += source.errors
    target.error_messages.extend(source.error_messages)
    target.documents_ingested += source.documents_ingested


async def build_graph_multi_tenant_async(
    checked_results: dict[str, list[CheckedResult]],
    articles: dict[str, list[Article]],
    financial_data: dict[str, FundamentalData],
    only_passed: bool = True,
    host: str | None = None,
    port: int | None = None,
) -> dict[int, GraphStats]:
    _build_provenance_snapshot(
        checked_results,
        articles,
        financial_data,
        only_passed,
    )
    docs = [
        *_documents_from_news(checked_results, articles, only_passed),
        *_documents_from_financial(financial_data),
    ]

    grouped: dict[int, list[IngestDocument]] = {}
    for doc in docs:
        grouped.setdefault(doc.year, []).append(doc)

    from app.services.database.graphrag_engine import GraphRAGEngine

    stats_by_year: dict[int, GraphStats] = {}
    async with GraphRAGEngine(host=host, port=port) as engine:
        for year, year_docs in sorted(grouped.items()):
            stats = await engine.ingest_documents(year, year_docs)
            stats_by_year[year] = stats
            print(
                f"[{stats.graph_name}] ingested={stats.documents_ingested}, "
                f"nodes={stats.nodes_created}, edges={stats.edges_created}, errors={stats.errors}"
            )

    successful_years = []
    for year, stats in stats_by_year.items():
        validation = validate_graph(year, host=host, port=port)
        if (
            stats.documents_ingested > 0
            and stats.errors == 0
            and validation.get("total_nodes", 0) > 0
        ):
            successful_years.append(year)
        else:
            stats.error_messages.append(
                "Graph tidak diregistrasikan karena hasil ingest kosong atau bermasalah."
            )
            stats.errors += 1
    _sync_registry_years(list_year_graphs(host, port))
    return stats_by_year


def build_graph_multi_tenant(
    checked_results: dict[str, list[CheckedResult]],
    articles: dict[str, list[Article]],
    financial_data: dict[str, FundamentalData],
    only_passed: bool = True,
    host: str | None = None,
    port: int | None = None,
) -> dict[int, GraphStats]:
    return asyncio.run(
        build_graph_multi_tenant_async(
            checked_results, articles, financial_data, only_passed, host, port,
        )
    )


def build_news_graph(
    checked_results: dict[str, list[CheckedResult]],
    articles: dict[str, list[Article]],
    only_passed: bool = True,
    host: str | None = None,
    port: int | None = None,
) -> dict[int, GraphStats]:
    return build_graph_multi_tenant(checked_results, articles, {}, only_passed, host, port)


def build_financial_graph(
    financial_data: dict[str, FundamentalData],
    host: str | None = None,
    port: int | None = None,
) -> dict[int, GraphStats]:
    return build_graph_multi_tenant({}, {}, financial_data, True, host, port)


def validate_graph(year: int, host: str | None = None, port: int | None = None) -> dict:
    graph_host = host or os.getenv("FALKORDB_HOST", "localhost")
    graph_port = port or int(os.getenv("FALKORDB_PORT", "6379"))
    graph_name = graph_name_for_year(year)
    try:
        client = FalkorDB(host=graph_host, port=graph_port)
        names = set(client.list_graphs())
        if graph_name not in names:
            return {
                "year": year,
                "graph_name": graph_name,
                "exists": False,
                "total_nodes": 0,
                "total_edges": 0,
                "available_years": list_year_graphs(graph_host, graph_port),
            }
        graph = client.select_graph(graph_name)
        nodes = int(graph.query("MATCH (n) RETURN count(n)").result_set[0][0])
        edges = int(
            graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
        )
        return {
            "year": year,
            "graph_name": graph_name,
            "exists": nodes > 0,
            "total_nodes": nodes,
            "total_edges": edges,
            "available_years": list_year_graphs(graph_host, graph_port),
        }
    except Exception as exc:
        return {
            "year": year,
            "graph_name": graph_name,
            "exists": False,
            "total_nodes": 0,
            "total_edges": 0,
            "available_years": [],
            "error": str(exc),
        }
