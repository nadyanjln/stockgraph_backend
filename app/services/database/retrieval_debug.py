"""Retrieval observability helpers for debugging RAGAS context precision."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEBUG_DIR = Path(os.getenv("STOCKGRAPH_RETRIEVAL_DEBUG_DIR", "evaluation/debug"))


def estimate_tokens(text: str) -> int:
    """Return a deterministic token estimate without adding tokenizer deps."""
    clean = " ".join(str(text or "").split())
    if not clean:
        return 0
    return max(1, round(len(clean) / 4))


def context_token_stats(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, item in enumerate(contexts, start=1):
        text = str(item.get("text") or item.get("context_preview") or "")
        output.append(
            {
                "context_number": index,
                "id": item.get("id") or item.get("source_id") or f"context-{index}",
                "ticker": item.get("ticker", ""),
                "type": item.get("source_type") or item.get("document_type") or "",
                "source": item.get("source") or item.get("source_document") or "",
                "characters": len(text),
                "tokens": estimate_tokens(text),
            }
        )
    return output


def prompt_stats(
    *,
    question: str,
    prompt: str,
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    context_text = "\n\n".join(
        str(item.get("text") or item.get("context_preview") or "")
        for item in contexts
    )
    return {
        "prompt_token_count": estimate_tokens(prompt),
        "context_token_count": estimate_tokens(context_text),
        "question_token_count": estimate_tokens(question),
        "included_contexts": context_token_stats(contexts),
    }


def build_answer_support(
    answer: str,
    contexts: list[dict[str, Any]],
    *,
    min_overlap: int = 2,
) -> list[dict[str, Any]]:
    """Map each answer sentence to likely supporting contexts using lexical overlap."""
    context_terms = []
    for index, item in enumerate(contexts, start=1):
        text = str(item.get("text") or item.get("context_preview") or "")
        context_terms.append((index, item, _terms(text)))

    rows: list[dict[str, Any]] = []
    for sentence_index, sentence in enumerate(_sentences(answer), start=1):
        sentence_terms = _terms(sentence)
        supported_by: list[dict[str, Any]] = []
        if sentence_terms:
            for context_index, item, terms in context_terms:
                overlap = sentence_terms.intersection(terms)
                if len(overlap) >= min_overlap:
                    supported_by.append(
                        {
                            "context_number": context_index,
                            "id": item.get("id")
                            or item.get("source_id")
                            or f"context-{context_index}",
                            "overlap_terms": sorted(overlap)[:12],
                        }
                    )
        rows.append(
            {
                "sentence_number": sentence_index,
                "sentence": sentence,
                "supported_by": supported_by,
                "unsupported": not supported_by,
            }
        )
    return rows


def export_question_debug(payload: dict[str, Any]) -> Path:
    """Write one JSON file per question under evaluation/debug."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = _next_debug_path(DEBUG_DIR)
    enriched = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    path.write_text(
        json.dumps(enriched, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def build_debug_payload(
    *,
    question: str,
    plan: Any,
    agent_diagnostics: dict[str, dict],
    sources: list[dict[str, str]],
    graph_paths: list[str],
    answer: str,
    final_prompt: str,
) -> dict[str, Any]:
    agent_debug = {
        name: values.get("retrieval_observability", {})
        for name, values in agent_diagnostics.items()
        if values.get("retrieval_observability")
    }
    final_contexts = _collect_final_contexts(agent_debug)
    return {
        "question": question,
        "plan": {
            "agents": getattr(plan, "agents", []),
            "year": getattr(plan, "year", None),
            "rationale": getattr(plan, "rationale", ""),
        },
        "agents": agent_debug,
        "top_k_summary": _top_k_summary(agent_debug),
        "final_prompt": prompt_stats(
            question=question,
            prompt=final_prompt,
            contexts=final_contexts,
        ),
        "answer_validation": build_answer_support(answer, final_contexts),
        "answer": answer,
        "sources": sources,
        "graph_paths": graph_paths,
    }


def log_observability(logger: Any, title: str, payload: Any) -> None:
    logger.info("")
    logger.info("------------------------------------------------")
    logger.info("%s", title)
    logger.info("------------------------------------------------")
    logger.info("%s", json.dumps(payload, ensure_ascii=False, default=str))


def _collect_final_contexts(agent_debug: dict[str, dict]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for agent_name, debug in agent_debug.items():
        for item in debug.get("final_contexts", []):
            context_id = str(item.get("id") or item.get("source_id") or item.get("context_preview") or "")
            if context_id in seen:
                continue
            seen.add(context_id)
            contexts.append({"agent": agent_name, **item})
    return contexts


def _top_k_summary(agent_debug: dict[str, dict]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for agent_name, debug in agent_debug.items():
        rows[agent_name] = {
            "retrieved": debug.get("counts", {}).get("retrieved", 0),
            "merged": debug.get("counts", {}).get("merged", 0),
            "deduplicated": debug.get("counts", {}).get("deduplicated", 0),
            "reranked": debug.get("counts", {}).get("reranked", 0),
            "sent_to_gpt": debug.get("counts", {}).get("sent_to_gpt", 0),
        }
    return rows


def _next_debug_path(directory: Path) -> Path:
    max_index = 0
    for path in directory.glob("sample_*.json"):
        match = re.search(r"sample_(\d+)\.json$", path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return directory / f"sample_{max_index + 1:03d}.json"


def _sentences(text: str) -> list[str]:
    compact = " ".join(str(text or "").split())
    if not compact:
        return []
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", compact)
        if sentence.strip()
    ]


def _terms(text: str) -> set[str]:
    stopwords = {
        "adalah",
        "akan",
        "atau",
        "bahwa",
        "dan",
        "dari",
        "dengan",
        "di",
        "ini",
        "itu",
        "ke",
        "pada",
        "sebagai",
        "yang",
        "untuk",
    }
    return {
        term
        for term in re.findall(r"[a-zA-Z0-9]{3,}", str(text).casefold())
        if term not in stopwords
    }


__all__ = [
    "build_answer_support",
    "build_debug_payload",
    "context_token_stats",
    "estimate_tokens",
    "export_question_debug",
    "log_observability",
    "prompt_stats",
]
