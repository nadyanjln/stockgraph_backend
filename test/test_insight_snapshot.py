from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.agent.insight_snapshot import build_conversation_insight_snapshot
from app.core.extractor.entity_validation import is_valid_entity
from app.core.extractor.llm_extractor import Entity, ExtractionResult
from app.core.extractor.relevance_checker import CheckedResult, RelevanceScore
from app.services.crawler.news_crawler import Article
from app.services.database import graph_builder


def checked_result(url: str, entities: list[Entity]) -> CheckedResult:
    extraction = ExtractionResult(
        stock_code="BRPT",
        source_type="news",
        source_ref=url,
        entities=entities,
    )
    return CheckedResult(
        extraction=extraction,
        score=RelevanceScore("BRPT", url, 0.9, 0.9, "relevan", True),
    )


class InsightSnapshotTests(unittest.TestCase):
    def test_query_noise_is_not_a_valid_entity(self) -> None:
        for label in (
            "site:kompas.com",
            "https://example.com",
            "cnnindonesia.com",
            "analisis laporan keuangan BRPT",
            "berita terbaru risiko BRPT",
        ):
            self.assertFalse(
                is_valid_entity(Entity(label, label, "EVENT"), has_evidence=True)
            )
        self.assertTrue(
            is_valid_entity(Entity("brpt", "BRPT", "STOCK"), has_evidence=True)
        )

    def test_snapshot_is_stable_and_uses_validated_news_only(self) -> None:
        articles = [
            Article(
                "BRPT",
                "BRPT mencatat pertumbuhan laba",
                "https://example.com/one",
                "kontan.co.id",
                "2026-06-20",
                "Laba meningkat dan bisnis tumbuh positif.",
            ),
            Article(
                "BRPT",
                "Kinerja BRPT kembali menguat",
                "https://example.com/two",
                "bisnis.com",
                "2026-06-21",
                "Pendapatan naik dan ekspansi membaik.",
            ),
        ]
        entities = [
            Entity("brpt", "BRPT", "STOCK"),
            Entity("bad", "site:kompas.com", "EVENT"),
        ]
        checked = {"BRPT": [checked_result(item.url, entities) for item in articles]}
        stats = {2026: SimpleNamespace(nodes_created=5, edges_created=4)}

        first = build_conversation_insight_snapshot(
            checked, {"BRPT": articles}, {}, stats
        )
        second = build_conversation_insight_snapshot(
            checked, {"BRPT": articles}, {}, stats
        )

        self.assertEqual(first["source_snapshot_id"], second["source_snapshot_id"])
        self.assertEqual(first["sentiment"], "positive")
        self.assertEqual(first["news_source_count"], 2)
        self.assertNotIn("bad", first["entity_ids"])
        self.assertTrue(set(first["source_ids"]))

    def test_insufficient_news_does_not_force_neutral_sentiment(self) -> None:
        article = Article(
            "BRPT",
            "Berita tunggal BRPT",
            "https://example.com/only",
            "kontan.co.id",
            "2026-06-20",
            "Informasi terbaru.",
        )
        result = build_conversation_insight_snapshot(
            {"BRPT": [checked_result(article.url, [Entity("brpt", "BRPT", "STOCK")])]},
            {"BRPT": [article]},
            {},
            {},
        )
        self.assertEqual(result["sentiment"], "insufficient_data")
        self.assertIn("Belum cukup berita", result["sentiment_reason"])

    def test_graph_registry_does_not_create_nodes_for_query_noise(self) -> None:
        article = Article(
            "BRPT",
            "Berita BRPT",
            "https://example.com/graph",
            "kontan.co.id",
            "2026-06-20",
            "BRPT menjalankan ekspansi.",
        )
        checked = {
            "BRPT": [
                checked_result(
                    article.url,
                    [
                        Entity("brpt", "BRPT", "STOCK"),
                        Entity("noise", "site:cnnindonesia.com", "EVENT"),
                    ],
                )
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            with patch.object(graph_builder, "PROVENANCE_PATH", path):
                registry = graph_builder._build_provenance_snapshot(
                    checked, {"BRPT": [article]}, {}, True
                )
        labels = {node["label"] for node in registry["nodes"].values()}
        self.assertIn("BRPT", labels)
        self.assertNotIn("site:cnnindonesia.com", labels)


if __name__ == "__main__":
    unittest.main()
