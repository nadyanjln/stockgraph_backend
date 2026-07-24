"""Build a RAGAS-compatible dataset from StockGraph retrieval debug logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_DEBUG_DIR = Path("evaluation/debug")
DEFAULT_OUTPUT = Path("evaluation/agent_dataset.json")


def build_dataset(
    debug_dir: str | Path = DEFAULT_DEBUG_DIR,
    output_path: str | Path = DEFAULT_OUTPUT,
    *,
    use_answer_as_ground_truth: bool = False,
) -> list[dict[str, Any]]:
    source_dir = Path(debug_dir)
    output = Path(output_path)
    rows: list[dict[str, Any]] = []

    for path in sorted(source_dir.glob("sample_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        question = _clean(payload.get("question"))
        answer = _clean(payload.get("answer"))
        contexts = _extract_contexts(payload)
        if not question or not answer or not contexts:
            continue
        rows.append(
            {
                "question": question,
                "answer": answer,
                "ground_truth": answer if use_answer_as_ground_truth else "",
                "contexts": contexts,
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return rows


def _extract_contexts(payload: dict[str, Any]) -> list[str]:
    contexts: list[str] = []
    seen: set[str] = set()
    for agent_debug in payload.get("agents", {}).values():
        for item in agent_debug.get("final_contexts", []):
            text = _clean(item.get("text") or item.get("context_preview"))
            if not text:
                continue
            fingerprint = " ".join(text.casefold().split())[:500]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            contexts.append(text)
    return contexts


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a RAGAS dataset from StockGraph evaluation/debug JSON logs."
    )
    parser.add_argument("--debug-dir", default=str(DEFAULT_DEBUG_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--use-answer-as-ground-truth",
        action="store_true",
        help=(
            "Use only for quick smoke tests. For final evaluation, manually write "
            "ground_truth from the exported contexts."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = build_dataset(
        args.debug_dir,
        args.output,
        use_answer_as_ground_truth=args.use_answer_as_ground_truth,
    )
    print(f"Wrote {len(rows)} samples to {args.output}")


if __name__ == "__main__":
    main()
