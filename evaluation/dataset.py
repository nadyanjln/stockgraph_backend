"""Dataset loading and validation for GraphRAG evaluation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from evaluation.utils import clean_optional_text, normalize_contexts


@dataclass(frozen=True)
class EvaluationSample:
    question: str
    answer: str
    contexts: list[str]
    ground_truth: str = ""

    @property
    def has_ground_truth(self) -> bool:
        return bool(self.ground_truth.strip())

    @property
    def has_contexts(self) -> bool:
        return bool(self.contexts)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


class DatasetValidationError(ValueError):
    """Raised when the evaluation dataset cannot be used."""


def load_evaluation_dataset(path: str | Path) -> list[EvaluationSample]:
    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"Dataset not found: {resolved}")

    if resolved.suffix.lower() == ".json":
        rows = _load_json(resolved)
    elif resolved.suffix.lower() == ".csv":
        rows = pd.read_csv(resolved).to_dict(orient="records")
    else:
        raise DatasetValidationError(
            "Dataset must be a .json or .csv file with question, answer, "
            "contexts, and optional ground_truth columns."
        )

    samples = [_normalize_row(row, index) for index, row in enumerate(rows, start=1)]
    if not samples:
        raise DatasetValidationError("Evaluation dataset is empty.")
    return samples


def samples_to_hf_dataset(samples: list[EvaluationSample]):
    """Return a Hugging Face Dataset for optional downstream RAGAS workflows."""
    try:
        from datasets import Dataset
    except ImportError as exc:  # pragma: no cover - dependency error path.
        raise RuntimeError(
            "datasets is required to build a Hugging Face Dataset. "
            "Install it with `pip install datasets`."
        ) from exc

    return Dataset.from_list([sample.to_record() for sample in samples])


def _load_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        raise DatasetValidationError(
            "JSON dataset must be a list of objects or an object with a data list."
        )
    return data


def _normalize_row(row: dict[str, Any], index: int) -> EvaluationSample:
    question = clean_optional_text(row.get("question"))
    answer = clean_optional_text(row.get("answer"))
    contexts = normalize_contexts(row.get("contexts"))
    ground_truth = clean_optional_text(
        row.get("ground_truth") or row.get("reference")
    )

    missing = []
    if not question:
        missing.append("question")
    if not answer:
        missing.append("answer")
    if missing:
        raise DatasetValidationError(
            f"Dataset row {index} is missing required field(s): {', '.join(missing)}"
        )

    return EvaluationSample(
        question=question,
        answer=answer,
        contexts=contexts,
        ground_truth=ground_truth,
    )
