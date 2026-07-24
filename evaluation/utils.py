"""Small helpers shared by the evaluation modules."""

from __future__ import annotations

import json
import logging
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

METRIC_COLUMNS = [
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
    "answer_correctness",
]


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )


def ensure_dir(path: str | Path) -> Path:
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def clear_directory(path: str | Path) -> Path:
    resolved = ensure_dir(path)
    for item in resolved.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    return resolved


def normalize_contexts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
        return [part.strip() for part in text.split("|||") if part.strip()]
    return [str(value).strip()] if str(value).strip() else []


def clean_optional_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def safe_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score):
        return None
    return score


def metric_averages(rows: Iterable[dict[str, Any]]) -> dict[str, float | None]:
    output: dict[str, float | None] = {}
    materialized = list(rows)
    for column in METRIC_COLUMNS:
        values = [
            score
            for row in materialized
            if (score := safe_score(row.get(column))) is not None
        ]
        output[column] = sum(values) / len(values) if values else None
    return output


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
