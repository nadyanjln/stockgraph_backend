"""Command line entry point for StockGraph GraphRAG evaluation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from evaluation.config import EvaluationConfig
from evaluation.evaluator import GraphRagasEvaluator
from evaluation.utils import configure_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate StockGraph GraphRAG outputs with RAGAS."
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        help="Path to JSON or CSV evaluation dataset.",
    )
    parser.add_argument(
        "--static",
        action="store_true",
        help="Use the pre-saved answers and contexts (the evaluator's default mode).",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    configure_logging()
    config = EvaluationConfig.from_env(
        dataset_path=args.dataset,
    )
    evaluator = GraphRagasEvaluator(config)
    await evaluator.run()


if __name__ == "__main__":
    asyncio.run(async_main())
