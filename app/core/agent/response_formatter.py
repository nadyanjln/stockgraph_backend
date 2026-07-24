"""Utilities to format RAG answers into frontend-friendly Markdown payloads."""

from __future__ import annotations

import re
from urllib.parse import urlparse

URL_REGEX = re.compile(r"https?://[^\s)\]>\"']+", flags=re.IGNORECASE)


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _source_title_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.strip("/")
    if not path:
        return host
    first_part = path.split("/")[0]
    return f"{host} / {first_part}"


def build_sources(
    citations: list[str] | None,
    retrieved_sources: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    if retrieved_sources:
        return [
            {
                "source_id": str(source.get("source_id") or f"source-{index}"),
                "source_type": str(source.get("source_type") or ""),
                "title": str(source.get("title") or source.get("url") or f"Sumber {index}"),
                "source_name": str(source.get("source_name") or ""),
                "url": str(source.get("url") or ""),
                "publication_date": str(source.get("publication_date") or ""),
                "reporting_period": str(source.get("reporting_period") or ""),
                "snippet": str(source.get("snippet") or "")[:700],
                "retrieved_text": str(source.get("retrieved_text") or "")[:2000],
            }
            for index, source in enumerate(retrieved_sources[:8], start=1)
        ]

    items = citations or []
    seen: set[str] = set()
    sources: list[dict[str, str]] = []

    for raw in items:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)

        extracted_urls = [url.rstrip(".,;:") for url in URL_REGEX.findall(value)]
        if _is_url(value):
            sources.append({
                "source_id": f"source-{len(sources) + 1}",
                "source_type": "news",
                "title": _source_title_from_url(value),
                "source_name": urlparse(value).netloc.replace("www.", ""),
                "url": value,
                "publication_date": "",
                "reporting_period": "",
                "snippet": "",
                "retrieved_text": "",
            })
        elif extracted_urls:
            first_url = extracted_urls[0]
            clean_title = re.sub(URL_REGEX, "", value).strip(" -:")
            sources.append({
                "source_id": f"source-{len(sources) + 1}",
                "source_type": "news",
                "title": clean_title or _source_title_from_url(first_url),
                "source_name": urlparse(first_url).netloc.replace("www.", ""),
                "url": first_url,
                "publication_date": "",
                "reporting_period": "",
                "snippet": "",
                "retrieved_text": "",
            })
        else:
            # Plain text is not sufficient provenance for a citation.
            continue

    return sources


def _remove_source_sections(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    skipping = False

    for line in lines:
        lower = line.strip().lower()
        if lower in {"## sumber", "### sumber", "## referensi", "### referensi", "sumber:", "referensi:"}:
            skipping = True
            continue
        if skipping and re.match(r"^\s*(\d+\.\s+|\-\s+|\*\s+)", line):
            continue
        if skipping and not line.strip():
            continue
        if skipping and line.strip():
            skipping = False
        output.append(line)

    return "\n".join(output).strip()


def _normalize_body(answer: str) -> str:
    cleaned = _remove_source_sections(re.sub(r"\n{3,}", "\n\n", answer.strip()))
    if not cleaned:
        return "Aku belum menemukan data yang cukup dari dokumen yang tersedia untuk menjawab ini secara pasti."
    return cleaned


def _sanitize_inline_citations(answer: str, source_count: int) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return match.group(0) if 1 <= index <= source_count else ""

    return re.sub(r"\[(\d+)\]", replace, answer)


def build_answer_markdown(answer: str, source_count: int = 0) -> str:
    return _normalize_body(_sanitize_inline_citations(answer, source_count))


def format_rag_response(
    answer: str,
    citations: list[str] | None,
    retrieved_sources: list[dict[str, str]] | None = None,
) -> dict:
    text = (answer or "").strip()
    sources = build_sources(citations, retrieved_sources)
    answer_markdown = build_answer_markdown(text, len(sources))
    return {
        "answer_markdown": answer_markdown,
        "sources": sources,
    }
