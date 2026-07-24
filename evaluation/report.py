"""Report generation for GraphRAG evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from evaluation.utils import METRIC_COLUMNS, ensure_dir, format_duration, metric_averages


def build_summary(
    rows: list[dict[str, Any]],
    *,
    elapsed_seconds: float,
) -> dict[str, Any]:
    samples = len(rows)
    success = sum(1 for row in rows if bool(row.get("success")))
    failed = samples - success
    averages = metric_averages(rows)

    return {
        "samples": samples,
        "success": success,
        "failed": failed,
        "success_percentage": (success / samples * 100.0) if samples else 0.0,
        "evaluation_time_seconds": elapsed_seconds,
        "evaluation_time": format_duration(elapsed_seconds),
        "averages": averages,
    }


def save_metrics_csv(rows: list[dict[str, Any]], results_dir: str | Path) -> Path:
    output_dir = ensure_dir(results_dir)
    path = output_dir / "metrics.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def save_metrics_json(rows: list[dict[str, Any]], results_dir: str | Path) -> Path:
    output_dir = ensure_dir(results_dir)
    path = output_dir / "metrics.json"
    path.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def save_summary_json(summary: dict[str, Any], results_dir: str | Path) -> Path:
    output_dir = ensure_dir(results_dir)
    path = output_dir / "summary.json"
    path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def print_summary(summary: dict[str, Any], results_dir: str | Path) -> None:
    averages = summary.get("averages", {})

    def value(name: str) -> str:
        score = averages.get(name)
        return "-" if score is None else f"{score:.2f}"

    labels = {
        "faithfulness": "Faithfulness",
        "answer_relevancy": "Answer Relevancy",
        "context_precision": "Context Precision",
        "context_recall": "Context Recall",
        "answer_correctness": "Answer Correctness",
    }

    print("==================================")
    print("GraphRAG Evaluation Summary")
    print("==================================")
    print()
    print(f"Samples               : {summary.get('samples', 0)}")
    print(f"Success               : {summary.get('success', 0)}")
    print(f"Failed                : {summary.get('failed', 0)}")
    print()
    for column in METRIC_COLUMNS:
        print(f"{labels[column]:<22}: {value(column)}")
    print()
    print(f"Evaluation Time       : {summary.get('evaluation_time', '0s')}")
    print()
    print("Results saved to:")
    print(str(Path(results_dir)))
