"""Production-oriented RAGAS evaluator for StockGraph GraphRAG outputs."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from app.core.openai_client import async_openai_client
from evaluation.config import EvaluationConfig
from evaluation.dataset import EvaluationSample, load_evaluation_dataset, samples_to_hf_dataset
from evaluation.report import (
    build_summary,
    print_summary,
    save_metrics_csv,
    save_metrics_json,
    save_summary_json,
)
from evaluation.utils import METRIC_COLUMNS, clear_directory, ensure_dir
from evaluation.visualize import plot_all_metrics

logger = logging.getLogger(__name__)


class GraphRagasEvaluator:
    """Evaluate GraphRAG answers with the current RAGAS collections API.

    The implementation intentionally avoids `ragas.evaluate()` because the
    current RAGAS docs mark it as deprecated in favor of async-first and
    collections-based metric scoring. Scoring metric-by-metric also lets a
    production evaluation continue when one sample or metric fails.
    """

    def __init__(self, config: EvaluationConfig | None = None) -> None:
        self.config = config or EvaluationConfig.from_env()
        self.samples: list[EvaluationSample] = []
        self.dataset: Any | None = None
        self.results: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}
        self._scorers: dict[str, Any] | None = None

    def load_dataset(self, path: str | Path | None = None) -> list[EvaluationSample]:
        dataset_path = Path(path or self.config.dataset_path)
        logger.info("Loading dataset...")
        self.samples = load_evaluation_dataset(dataset_path)
        logger.info("Loaded %s evaluation samples from %s", len(self.samples), dataset_path)
        return self.samples

    def prepare_dataset(self):
        if not self.samples:
            self.load_dataset()
        logger.info("Preparing Hugging Face Dataset...")
        self.dataset = samples_to_hf_dataset(self.samples)
        return self.dataset

    async def evaluate(self) -> list[dict[str, Any]]:
        if not self.samples:
            self.load_dataset()
        if not self.samples:
            raise ValueError("Evaluation dataset is empty.")
        if not self.config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for RAGAS evaluation.")

        self._scorers = self._create_scorers()
        started_at = time.perf_counter()
        rows: list[dict[str, Any]] = []
        total = len(self.samples)

        logger.info("Evaluation started.")
        for index, sample in enumerate(self.samples, start=1):
            logger.info("Evaluating sample %s/%s...", index, total)
            row = await self._evaluate_sample(index, sample)
            rows.append(row)

        elapsed = time.perf_counter() - started_at
        self.results = rows
        self.summary = build_summary(rows, elapsed_seconds=elapsed)
        logger.info("Evaluation finished.")
        return rows

    def save_csv(self, path: str | Path | None = None) -> Path:
        logger.info("Saving CSV report...")
        output_dir = ensure_dir(path or self.config.results_dir)
        return save_metrics_csv(self.results, output_dir)

    def save_json(self, path: str | Path | None = None) -> Path:
        logger.info("Saving JSON report...")
        output_dir = ensure_dir(path or self.config.results_dir)
        metrics_path = save_metrics_json(self.results, output_dir)
        save_summary_json(self.summary, output_dir)
        return metrics_path

    def generate_summary(self) -> dict[str, Any]:
        if not self.summary:
            self.summary = build_summary(self.results, elapsed_seconds=0.0)
        return self.summary

    def plot_metrics(self, path: str | Path | None = None) -> list[Path]:
        logger.info("Saving visualizations...")
        return plot_all_metrics(self.results, path or self.config.results_dir)

    async def run(self) -> dict[str, Any]:
        self.load_dataset()
        self.prepare_dataset()
        await self.evaluate()
        logger.info("Clearing previous evaluation results...")
        clear_directory(self.config.results_dir)
        self.save_csv()
        self.save_json()
        self.plot_metrics()
        summary = self.generate_summary()
        print_summary(summary, self.config.results_dir)
        return summary

    def _create_scorers(self) -> dict[str, Any]:
        try:
            from ragas.embeddings.base import embedding_factory
            from ragas.llms import llm_factory
            from ragas.metrics.collections import (
                AnswerRelevancy,
                ContextPrecision,
                ContextRecall,
                ContextUtilization,
                Faithfulness,
                FactualCorrectness,
            )
        except ImportError as exc:  # pragma: no cover - dependency error path.
            raise RuntimeError(
                "RAGAS evaluation dependencies are not installed. "
                "Install ragas, datasets, pandas, matplotlib, and openai."
            ) from exc

        client = async_openai_client().with_options(
            timeout=self.config.request_timeout_seconds,
            max_retries=self.config.max_retries,
        )
        llm = llm_factory(self.config.openai_model, client=client)
        embeddings = embedding_factory(
            "openai",
            model=self.config.embedding_model,
            client=client,
        )

        return {
            "faithfulness": Faithfulness(llm=llm),
            "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
            "context_precision": ContextPrecision(llm=llm),
            # RAGAS ContextRecall requires a reference answer.
            "context_recall": ContextRecall(llm=llm),
            # RAGAS collections API exposes FactualCorrectness for generated
            # response vs reference comparison. This is the current replacement
            # for legacy "answer correctness" style evaluation.
            "answer_correctness": FactualCorrectness(llm=llm),
            # When ground_truth is missing, ContextPrecision cannot be computed
            # with a reference; RAGAS recommends ContextUtilization for that case.
            "context_utilization": ContextUtilization(llm=llm),
        }

    async def _evaluate_sample(
        self,
        index: int,
        sample: EvaluationSample,
    ) -> dict[str, Any]:
        assert self._scorers is not None

        row: dict[str, Any] = {
            "sample_id": index,
            "question": sample.question,
            "answer": sample.answer,
            "ground_truth": sample.ground_truth,
            "contexts_count": len(sample.contexts),
            "success": False,
            "error": "",
            "context_precision_source": "",
        }
        for column in METRIC_COLUMNS:
            row[column] = None

        errors: list[str] = []
        if not sample.has_contexts:
            errors.append("contexts empty: skipped context-dependent metrics")
        else:
            row["faithfulness"] = await self._score_metric(
                "faithfulness",
                errors,
                user_input=sample.question,
                response=sample.answer,
                retrieved_contexts=sample.contexts,
            )

            if sample.has_ground_truth:
                row["context_precision_source"] = "ContextPrecision"
                row["context_precision"] = await self._score_metric(
                    "context_precision",
                    errors,
                    user_input=sample.question,
                    reference=sample.ground_truth,
                    retrieved_contexts=sample.contexts,
                )
            else:
                row["context_precision_source"] = "ContextUtilization"
                row["context_precision"] = await self._score_metric(
                    "context_utilization",
                    errors,
                    user_input=sample.question,
                    response=sample.answer,
                    retrieved_contexts=sample.contexts,
                )

            if sample.has_ground_truth:
                row["context_recall"] = await self._score_metric(
                    "context_recall",
                    errors,
                    user_input=sample.question,
                    reference=sample.ground_truth,
                    retrieved_contexts=sample.contexts,
                )
            else:
                errors.append("ground_truth missing: skipped context_recall")

        row["answer_relevancy"] = await self._score_metric(
            "answer_relevancy",
            errors,
            user_input=sample.question,
            response=sample.answer,
        )

        if sample.has_ground_truth:
            row["answer_correctness"] = await self._score_metric(
                "answer_correctness",
                errors,
                response=sample.answer,
                reference=sample.ground_truth,
            )
        else:
            errors.append("ground_truth missing: skipped answer_correctness")

        row["success"] = any(row.get(column) is not None for column in METRIC_COLUMNS)
        row["error"] = "; ".join(errors)
        return row

    async def _score_metric(
        self,
        metric_name: str,
        errors: list[str],
        **kwargs: Any,
    ) -> float | None:
        assert self._scorers is not None
        scorer = self._scorers[metric_name]
        try:
            logger.info(
                "openai_request=%s",
                {
                    "file_function": "evaluation.evaluator.GraphRagasEvaluator._score_metric",
                    "purpose": f"RAGAS Metric: {metric_name}",
                    "endpoint": "ragas.metric.ascore",
                    "model": self.config.openai_model,
                },
            )
            result = await asyncio.wait_for(
                scorer.ascore(**kwargs),
                timeout=self.config.request_timeout_seconds,
            )
            return float(result.value)
        except Exception as exc:  # noqa: BLE001 - continue per metric.
            logger.warning("Metric %s failed: %s", metric_name, exc)
            errors.append(f"{metric_name} failed: {type(exc).__name__}: {exc}")
            return None
