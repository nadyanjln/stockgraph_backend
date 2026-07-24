"""Validation helpers that keep retrieval/query noise out of the graph."""

from __future__ import annotations

import re

from app.core.extractor.llm_extractor import Entity

VALID_ENTITY_TYPES = {"PERSON", "ORGANIZATION", "POLICY", "STOCK", "EVENT"}
_QUERY_NOISE = re.compile(
    r"(^|\s)(site:|inurl:|intitle:|filetype:|https?://|www\.)"
    r"|(\.(com|co\.id|id|net|org)(/|\s|$))",
    re.IGNORECASE,
)
_INSTRUCTION_NOISE = {
    "analisis",
    "berita terbaru",
    "risiko investasi",
    "laporan keuangan terbaru",
    "search query",
    "crawler",
}


def is_valid_entity(entity: Entity, *, has_evidence: bool = True) -> bool:
    label = " ".join((entity.name or "").split()).strip()
    if not has_evidence or not entity.id or not label:
        return False
    if entity.type.upper() not in VALID_ENTITY_TYPES:
        return False
    if len(label) < 2 or len(label) > 120:
        return False
    if _QUERY_NOISE.search(label):
        return False
    lowered = label.casefold()
    if lowered in _INSTRUCTION_NOISE:
        return False
    if any(
        phrase in lowered
        for phrase in (
            "site:",
            "berita terbaru risiko",
            "analisis laporan keuangan",
            "query pencarian",
        )
    ):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]", label))


def normalized_entity_key(entity: Entity) -> str:
    return re.sub(r"[^a-z0-9]+", "-", entity.name.casefold()).strip("-")
