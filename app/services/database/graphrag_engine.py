"""
GraphRAG-SDK Engine — multi-tenant per-tahun.

Menggunakan FalkorDB GraphRAG-SDK untuk ekstraksi & query graph.
Tiap tahun (mis. 2023, 2024, 2025) punya KnowledgeGraph terpisah dengan ontology
sama (BEI_SCHEMA), sehingga query bisa di-scope per tahun fiskal.

Resource lifecycle:
  - Engine mempertahankan satu connection pool (FalkorDB) untuk semua tahun
  - GraphRAG instance lazy-init per tahun (di-cache)
  - Pakai async context manager via finalize() saat shutdown
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from graphrag_sdk import (
    ConnectionConfig,
    GraphRAG,
    LiteLLM,
    LiteLLMEmbedder,
)

from app.services.database.graph_builder import (
    GRAPH_PREFIX,
    GraphStats,
    IngestDocument,
    graph_name_for_year,
    list_year_graphs,
)
from app.utils.logger import get_logger

load_dotenv()

URL_REGEX = re.compile(r"https?://[^\s)\]>\"']+", flags=re.IGNORECASE)
logger = get_logger("stockgraph.graphrag")

try:
    from app.services.database.schema import BEI_SCHEMA
except Exception:
    BEI_SCHEMA = None


@dataclass
class QueryResult:
    question: str
    answer: str
    year: int
    context: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)


class GraphRAGEngine:
    """
    Multi-tenant GraphRAG manager.

    Usage:
        engine = GraphRAGEngine()
        await engine.initialize()  # lazy, hanya tahun yang ada di FalkorDB
        result = await engine.query("Bagaimana performa BBCA?", year=2024)
        await engine.close()

    Atau pakai sebagai async context manager:
        async with GraphRAGEngine() as engine:
            ...
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        llm_model: str = "openai/gpt-4o-mini",
        embedder_model: str = "openai/text-embedding-3-small",
        embedder_dim: int = 256,
    ):
        self._host = host or os.getenv("FALKORDB_HOST", "localhost")
        self._port = port or int(os.getenv("FALKORDB_PORT", 6379))
        self._llm_model = llm_model
        self._embedder_model = embedder_model
        self._embedder_dim = embedder_dim
        self._instances: dict[int, GraphRAG] = {}
        self._query_cache: dict[tuple[int, str, bool], QueryResult] = {}
        self._query_inflight: dict[tuple[int, str, bool], asyncio.Task[Any]] = {}
        self._available_years: list[int] = []
        self._is_available = False
        self._last_health_check = 0.0
        self._health_check_interval = float(
            os.getenv("FALKORDB_HEALTH_CHECK_INTERVAL", "5")
        )
        self._connect_timeout = float(os.getenv("FALKORDB_CONNECT_TIMEOUT", "0.5"))
        self._ingest_timeout = float(
            os.getenv("STOCKGRAPH_INGEST_TIMEOUT_SECONDS", "180")
        )
        self._finalize_timeout = float(
            os.getenv("STOCKGRAPH_FINALIZE_TIMEOUT_SECONDS", "90")
        )
        self._connection_error = ""

    async def __aenter__(self) -> "GraphRAGEngine":
        await self.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def initialize(self) -> None:
        """Discover tahun-tahun graph yang sudah ada di FalkorDB."""
        registry_years = list_year_graphs(self._host, self._port)
        if await self._ensure_available(force=True):
            self._available_years = registry_years
            logger.info("FalkorDB connected; available years=%s", self._available_years)
        else:
            self._available_years = []
            logger.warning(
                "FalkorDB unavailable at %s:%s; graph retrieval is disabled until "
                "the service becomes reachable",
                self._host,
                self._port,
            )

    @property
    def available_years(self) -> list[int]:
        return self._available_years

    @property
    def is_available(self) -> bool:
        return self._is_available

    @property
    def connection_error(self) -> str:
        return self._connection_error

    def _check_connection(self) -> tuple[bool, str]:
        try:
            with socket.create_connection(
                (self._host, self._port),
                timeout=self._connect_timeout,
            ):
                return True, ""
        except OSError as exc:
            return False, str(exc)

    async def _ensure_available(self, force: bool = False) -> bool:
        now = time.monotonic()
        if (
            not force
            and now - self._last_health_check < self._health_check_interval
        ):
            return self._is_available

        was_available = self._is_available
        self._last_health_check = now
        available, error = await asyncio.to_thread(self._check_connection)
        self._is_available = available
        self._connection_error = error

        if available and not was_available:
            self._available_years = list_year_graphs(self._host, self._port)
            logger.info("FalkorDB connection restored at %s:%s", self._host, self._port)
        elif not available and was_available:
            logger.warning("FalkorDB connection lost at %s:%s", self._host, self._port)

        return available

    async def _get_or_create(self, year: int) -> GraphRAG:
        """Lazy-init GraphRAG instance untuk satu tahun (cached)."""
        if not await self._ensure_available():
            raise ConnectionError(
                f"FalkorDB tidak tersedia di {self._host}:{self._port}"
            )
        if year in self._instances:
            return self._instances[year]

        kwargs = {
            "connection": ConnectionConfig(
                host=self._host,
                port=self._port,
                graph_name=graph_name_for_year(year),
            ),
            "llm": LiteLLM(model=self._llm_model),
            "embedder": LiteLLMEmbedder(
                model=self._embedder_model, dimensions=self._embedder_dim,
            ),
        }
        if BEI_SCHEMA is not None:
            kwargs["schema"] = BEI_SCHEMA
        try:
            rag = GraphRAG(**kwargs)
        except TypeError:
            kwargs.pop("schema", None)
            rag = GraphRAG(**kwargs)
        await rag.__aenter__()
        self._instances[year] = rag
        return rag

    async def ingest_articles(
        self, year: int, articles: list[tuple[str, str]],
    ) -> None:
        """
        Ingest list (document_id, text) ke knowledge graph tahun-tahun tertentu.

        Args:
            year:     tahun fiskal target
            articles: list (id, text) — biasanya url + body artikel
        """
        rag = await self._get_or_create(year)
        for doc_id, text in articles:
            try:
                await self._ingest_text(rag, text, doc_id)
            except Exception as exc:
                print(f"[GraphRAG {year}] ingest fail {doc_id[:60]}: {exc}")
        await rag.finalize()
        self._query_cache.clear()

    async def ingest_documents(
        self,
        year: int,
        documents: list[IngestDocument],
    ) -> GraphStats:
        """Ingest prepared domain documents using GraphRAG-SDK ``rag.ingest``."""
        stats = GraphStats(graph_name=graph_name_for_year(year))
        if not await self._ensure_available():
            stats.errors = 1
            stats.error_messages.append(
                f"FalkorDB tidak tersedia di {self._host}:{self._port}"
            )
            return stats

        rag = await self._get_or_create(year)

        for doc in documents:
            try:
                result = await asyncio.wait_for(
                    self._ingest_text(rag, doc.text, doc.document_id),
                    timeout=self._ingest_timeout,
                )
                stats.documents_ingested += 1
                stats.nodes_created += int(getattr(result, "nodes_created", 0) or 0)
                stats.edges_created += int(
                    getattr(result, "relationships_created", None)
                    or getattr(result, "edges_created", 0)
                    or 0
                )
            except Exception as exc:
                stats.errors += 1
                stats.error_messages.append(f"{doc.document_id}: {exc}")

        try:
            await asyncio.wait_for(rag.finalize(), timeout=self._finalize_timeout)
            self._query_cache.clear()
        except Exception as exc:
            stats.errors += 1
            stats.error_messages.append(f"finalize: {exc}")

        return stats

    async def _ingest_text(self, rag: GraphRAG, text: str, document_id: str):
        """Call GraphRAG-SDK ingest across minor API variants."""
        logger.info(
            "openai_request=%s",
            {
                "file_function": "app.services.database.graphrag_engine.GraphRAGEngine._ingest_text",
                "purpose": "GraphRAG Entity/Relation Extraction and Embedding",
                "endpoint": "graphrag_sdk.rag.ingest",
                "model": self._llm_model,
                "embedder_model": self._embedder_model,
                "document_id": document_id,
                "document_chars": len(text),
            },
        )
        try:
            return await rag.ingest(text=text, document_id=document_id)
        except TypeError:
            try:
                return await rag.ingest(document_id, text=text)
            except TypeError:
                return await rag.ingest(text)

    async def _completion_once(
        self,
        rag: GraphRAG,
        cache_key: tuple[int, str, bool],
        question: str,
        return_context: bool,
    ) -> Any:
        """Share one in-flight GraphRAG completion for identical query requests."""
        existing = self._query_inflight.get(cache_key)
        if existing is not None:
            logger.info(
                "graphrag_query_inflight_hit=%s",
                {
                    "year": cache_key[0],
                    "return_context": return_context,
                    "question_chars": len(question),
                },
            )
            return await asyncio.shield(existing)

        logger.info(
            "openai_request=%s",
            {
                "file_function": "app.services.database.graphrag_engine.GraphRAGEngine.query",
                "purpose": "GraphRAG Semantic Retrieval",
                "endpoint": "graphrag_sdk.rag.completion",
                "model": self._llm_model,
                "embedder_model": self._embedder_model,
                "year": cache_key[0],
                "return_context": return_context,
            },
        )
        task = asyncio.create_task(
            rag.completion(question, return_context=return_context)
        )
        self._query_inflight[cache_key] = task

        def clear_finished(done: asyncio.Task[Any]) -> None:
            if self._query_inflight.get(cache_key) is done:
                self._query_inflight.pop(cache_key, None)

        task.add_done_callback(clear_finished)
        return await asyncio.shield(task)

    async def query(
        self,
        question: str,
        year: int | None = None,
        return_context: bool = True,
    ) -> QueryResult:
        """
        Query knowledge graph satu tahun. Default: tahun terbaru yang tersedia.
        """
        target_year = year or (self._available_years[-1] if self._available_years else 0)
        if not await self._ensure_available():
            return QueryResult(
                question=question,
                answer=(
                    f"FalkorDB tidak tersedia di {self._host}:{self._port}; "
                    "konteks knowledge graph belum dapat diambil."
                ),
                year=target_year,
            )

        if not self._available_years:
            self._available_years = list_year_graphs(self._host, self._port)
            target_year = year or (
                self._available_years[-1] if self._available_years else 0
            )

        if target_year == 0:
            return QueryResult(
                question=question, answer="Belum ada data graph.", year=0,
            )

        cache_key = (target_year, question, return_context)
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            logger.info(
                "graphrag_query_cache_hit=%s",
                {
                    "year": target_year,
                    "return_context": return_context,
                    "question_chars": len(question),
                },
            )
            return cached

        rag = await self._get_or_create(target_year)
        try:
            response = await self._completion_once(
                rag,
                cache_key,
                question,
                return_context,
            )
        except Exception as exc:
            return QueryResult(
                question=question,
                answer=f"[Error query year={target_year}]: {exc}",
                year=target_year,
            )

        ctx_lines: list[str] = []
        citations: list[str] = []
        sources: list[dict[str, str]] = []

        def collect_urls(text: str) -> None:
            for match in URL_REGEX.findall(text or ""):
                cleaned = match.rstrip(".,;:")
                if cleaned:
                    citations.append(cleaned)

        def metadata_line(text: str, label: str) -> str:
            match = re.search(
                rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$",
                text,
            )
            return match.group(1).strip() if match else ""

        def add_source(
            *,
            source_type: str = "",
            title: str = "",
            source_name: str = "",
            url: str = "",
            publication_date: str = "",
            snippet: str = "",
            retrieved_text: str = "",
            source_id: str = "",
            reporting_period: str = "",
        ) -> None:
            clean_url = url.strip()
            clean_title = title.strip() or source_name.strip() or clean_url
            clean_snippet = snippet.strip()[:700]
            clean_retrieved = retrieved_text.strip()[:2000]
            key = (source_id or clean_url or clean_title or clean_snippet).lower()
            if not key:
                return
            if any(item["_key"] == key for item in sources):
                return
            sources.append({
                "_key": key,
                "source_id": source_id.strip() or f"source-{len(sources) + 1}",
                "source_type": source_type.strip(),
                "title": clean_title or f"Sumber {len(sources) + 1}",
                "source_name": source_name.strip(),
                "url": clean_url,
                "publication_date": publication_date.strip(),
                "reporting_period": reporting_period.strip(),
                "snippet": clean_snippet,
                "retrieved_text": clean_retrieved or clean_snippet,
            })

        ctx = getattr(response, "context", None)
        if ctx:
            for item in ctx:
                if isinstance(item, str):
                    ctx_lines.append(item)
                    collect_urls(item)
                    urls = URL_REGEX.findall(item)
                    url = metadata_line(item, "URL") or (
                        urls[0].rstrip(".,;:") if urls else ""
                    )
                    add_source(
                        source_type=(
                            "financial_report"
                            if "laporan keuangan" in item.lower()
                            or "tahun fiskal" in item.lower()
                            else "news"
                            if "berita" in item.lower() or url
                            else ""
                        ),
                        source_id=metadata_line(item, "ID") or url,
                        title=(
                            metadata_line(item, "Judul")
                            or metadata_line(item, "Nama perusahaan")
                            or url
                            or f"Graph context {len(sources) + 1}"
                        ),
                        source_name=metadata_line(item, "Sumber"),
                        url=url,
                        publication_date=(
                            metadata_line(item, "Tanggal publikasi")
                        ),
                        reporting_period=metadata_line(item, "Tahun fiskal"),
                        snippet=item,
                        retrieved_text=item,
                    )
                elif isinstance(item, dict):
                    src = item.get("source") or item.get("document_id")
                    for key in ("url", "source_url", "link", "document_id", "source"):
                        value = item.get(key)
                        if value:
                            citations.append(str(value))
                            collect_urls(str(value))
                    if src:
                        citations.append(str(src))
                    snippet = item.get("text") or str(item)
                    ctx_lines.append(snippet[:500])
                    collect_urls(snippet)
                    add_source(
                        source_type=str(
                            item.get("source_type")
                            or (
                                "financial_report"
                                if item.get("reporting_period")
                                else "news"
                                if item.get("url") or item.get("publisher")
                                else ""
                            )
                        ),
                        source_id=str(item.get("source_id") or item.get("document_id") or ""),
                        title=str(item.get("title") or item.get("name") or src or ""),
                        source_name=str(
                            item.get("source_name")
                            or item.get("publisher")
                            or item.get("source")
                            or ""
                        ),
                        url=str(
                            item.get("url")
                            or item.get("source_url")
                            or item.get("link")
                            or ""
                        ),
                        publication_date=str(
                            item.get("publication_date")
                            or item.get("published")
                            or item.get("date")
                            or ""
                        ),
                        reporting_period=str(item.get("reporting_period") or ""),
                        snippet=str(item.get("snippet") or snippet),
                        retrieved_text=str(item.get("retrieved_text") or snippet),
                    )

        clean_sources = [{k: v for k, v in item.items() if k != "_key"} for item in sources[:8]]

        result = QueryResult(
            question=question,
            answer=getattr(response, "answer", str(response)),
            year=target_year,
            context=ctx_lines[:8],
            citations=list(dict.fromkeys(citations))[:8],
            sources=clean_sources,
        )
        if len(self._query_cache) >= 64:
            self._query_cache.pop(next(iter(self._query_cache)))
        self._query_cache[cache_key] = result
        return result

    async def close(self) -> None:
        """Cleanup semua GraphRAG instances."""
        for rag in self._instances.values():
            try:
                await rag.__aexit__(None, None, None)
            except Exception:
                pass
        for task in self._query_inflight.values():
            task.cancel()
        self._query_inflight.clear()
        self._instances.clear()


def reset_graph(year: int, host: str | None = None, port: int | None = None) -> None:
    """Deprecated placeholder.

    Graph lifecycle should be managed by GraphRAG-SDK/FalkorDB administration
    tooling. The application no longer issues raw graph deletion operations.
    """
    _ = (year, host, port)
    print("[reset] skipped; manage graph deletion via FalkorDB/GraphRAG-SDK admin tooling")


__all__ = ["GraphRAGEngine", "QueryResult", "reset_graph", "GRAPH_PREFIX"]
