from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

from app.core.agent.evidence_policy import (
    coverage_instruction,
    no_evidence_answer,
    source_coverage,
)
from app.core.agent.response_formatter import format_rag_response
from app.services.database.evidence_retriever import retrieve_local_evidence
from app.services.database.graphrag_engine import GraphRAGEngine


def fixture_registry(*, news: bool = True, financial: bool = True) -> dict:
    articles = {}
    nodes = {
        "stock:BBCA": {
            "id": "stock:BBCA",
            "label": "BBCA",
            "type": "stock",
            "stock_codes": ["BBCA"],
            "source_ids": [],
        }
    }
    edges = {}
    if news:
        articles["article:1"] = {
            "id": "article:1",
            "title": "BBCA membukukan perkembangan bisnis terbaru",
            "publisher": "kontan.co.id",
            "publication_date": "2026-05-10",
            "url": "https://example.com/bbca-news",
            "summary": "Berita mengenai pertumbuhan kredit dan kondisi pasar.",
            "stock_codes": ["BBCA"],
            "year": 2026,
        }
        nodes["article:1"] = {
            "id": "article:1",
            "label": "Berita BBCA",
            "type": "article",
            "stock_codes": ["BBCA"],
            "source_ids": ["article:1"],
        }
        edges["edge:news"] = {
            "id": "edge:news",
            "source": "article:1",
            "target": "stock:BBCA",
            "type": "COVERS",
            "source_ids": ["article:1"],
        }
    if financial:
        nodes["metric:profit"] = {
            "id": "metric:profit",
            "label": "Net Profit 2025",
            "type": "financial",
            "description": "Net Profit: 54000000000000",
            "stock_codes": ["BBCA"],
            "source_ids": [],
        }
        edges["edge:financial"] = {
            "id": "edge:financial",
            "source": "stock:BBCA",
            "target": "metric:profit",
            "type": "REPORTS_FINANCIAL",
            "source_ids": [],
        }
    return {"articles": articles, "nodes": nodes, "edges": edges}


class EvidencePipelineTests(unittest.TestCase):
    def retrieve(self, registry: dict):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"STOCKGRAPH_PROVENANCE_REGISTRY": str(path)},
                clear=False,
            ), patch(
                "app.services.database.evidence_retriever.list_year_graphs",
                return_value=[],
            ):
                return retrieve_local_evidence(
                    "Faktor apa yang memengaruhi prospek Bank Central Asia (BBCA)?",
                    target_year=2026,
                    max_hops=2,
                )

    def test_bbca_with_news_and_financial_evidence(self):
        bundle = self.retrieve(fixture_registry())
        self.assertEqual(bundle.ticker, "BBCA")
        self.assertTrue(bundle.news_sources)
        self.assertTrue(bundle.financial_sources)
        self.assertGreaterEqual(len(bundle.graph_paths), 2)
        self.assertIsNone(bundle.diagnostics.fallback_reason)
        self.assertEqual(bundle.diagnostics.corpus_status, "evidence_available")
        self.assertEqual(
            bundle.diagnostics.graph_ingestion_status,
            "provenance_available_graph_empty",
        )

    def test_financial_only_still_has_fundamental_coverage(self):
        bundle = self.retrieve(fixture_registry(news=False))
        coverage = source_coverage(bundle.financial_sources)
        self.assertTrue(coverage["financial"])
        self.assertFalse(coverage["news"])
        self.assertIn("analisis fundamental", coverage_instruction(bundle.financial_sources))

    def test_news_only_mentions_specific_financial_limitation(self):
        bundle = self.retrieve(fixture_registry(financial=False))
        coverage = source_coverage(bundle.news_sources)
        self.assertTrue(coverage["news"])
        self.assertFalse(coverage["financial"])
        self.assertIn("validasi fundamental terbatas", coverage_instruction(bundle.news_sources))

    def test_empty_corpus_has_no_citations_or_invented_details(self):
        bundle = self.retrieve(fixture_registry(news=False, financial=False))
        answer = no_evidence_answer("Prospek BBCA?")
        formatted = format_rag_response(answer, [], [])
        self.assertEqual(formatted["sources"], [])
        self.assertNotRegex(formatted["answer_markdown"], r"\[\d+\]")
        self.assertEqual(bundle.diagnostics.fallback_reason, "no_valid_evidence_in_provenance_corpus")

    def test_citations_are_limited_to_retrieved_sources_and_labeled(self):
        sources = [
            {
                "source_id": "news:1",
                "source_type": "news",
                "title": "Berita nyata",
                "source_name": "Publisher",
                "url": "https://example.com/news",
                "publication_date": "2026-05-10",
                "reporting_period": "",
                "snippet": "Evidence berita",
                "retrieved_text": "Evidence berita",
            },
            {
                "source_id": "financial:BBCA:2025",
                "source_type": "financial_report",
                "title": "Laporan Keuangan BBCA",
                "source_name": "BBCA",
                "url": "",
                "publication_date": "",
                "reporting_period": "FY 2025",
                "snippet": "Net Profit 2025",
                "retrieved_text": "Net Profit 2025",
            },
        ]
        formatted = format_rag_response("Klaim berita [1], laporan [2], palsu [3].", [], sources)
        self.assertIn("[1]", formatted["answer_markdown"])
        self.assertIn("[2]", formatted["answer_markdown"])
        self.assertNotIn("[3]", formatted["answer_markdown"])
        self.assertEqual(formatted["sources"][0]["source_type"], "news")
        self.assertEqual(formatted["sources"][1]["source_type"], "financial_report")

    def test_multi_hop_query_traverses_graph(self):
        registry = fixture_registry()
        registry["nodes"]["event:credit"] = {
            "id": "event:credit",
            "label": "Pertumbuhan Kredit",
            "type": "topic",
            "stock_codes": ["BBCA"],
            "source_ids": ["article:1"],
        }
        registry["edges"]["edge:event"] = {
            "id": "edge:event",
            "source": "article:1",
            "target": "event:credit",
            "type": "MENTIONS",
            "source_ids": ["article:1"],
        }
        bundle = self.retrieve(registry)
        self.assertGreaterEqual(bundle.diagnostics.graph_edges_traversed, 3)
        self.assertTrue(any("Pertumbuhan Kredit" in path for path in bundle.graph_paths))


class GraphRAGIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_text_ingest_uses_keyword_document_id_signature(self):
        rag = type("DummyRag", (), {})()
        rag.ingest = AsyncMock(return_value=object())
        engine = GraphRAGEngine()

        await engine._ingest_text(rag, "BBCA evidence", "doc:bbca")

        rag.ingest.assert_awaited_once_with(
            text="BBCA evidence",
            document_id="doc:bbca",
        )


if __name__ == "__main__":
    unittest.main()
