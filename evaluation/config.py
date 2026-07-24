"""Configuration for StockGraph GraphRAG evaluation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_PATH = PROJECT_ROOT / "evaluation" / "sample_dataset.json"
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"


@dataclass(frozen=True)
class EvaluationConfig:
    dataset_path: Path
    results_dir: Path
    openai_api_key: str
    openai_model: str
    embedding_model: str
    request_timeout_seconds: float
    max_retries: int

    @classmethod
    def from_env(
        cls,
        *,
        dataset_path: str | Path | None = None,
    ) -> "EvaluationConfig":
        load_dotenv(PROJECT_ROOT / ".env")

        resolved_dataset = Path(
            dataset_path
            or os.getenv("RAGAS_EVAL_DATASET")
            or DEFAULT_DATASET_PATH
        )
        return cls(
            dataset_path=resolved_dataset,
            results_dir=DEFAULT_RESULTS_DIR,
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            embedding_model=os.getenv(
                "OPENAI_EMBEDDING_MODEL",
                "text-embedding-3-small",
            ),
            request_timeout_seconds=float(
                os.getenv("RAGAS_EVAL_TIMEOUT_SECONDS", "120")
            ),
            max_retries=int(os.getenv("RAGAS_EVAL_MAX_RETRIES", "2")),
        )
