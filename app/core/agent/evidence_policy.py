"""Evidence coverage and fallback policy shared by orchestrator and tests."""

from __future__ import annotations

import re


def source_coverage(sources: list[dict[str, str]]) -> dict[str, bool]:
    kinds = {str(source.get("source_type") or "") for source in sources}
    return {
        "news": "news" in kinds,
        "financial": "financial_report" in kinds,
    }


def no_evidence_answer(question: str) -> str:
    ticker_match = re.search(r"\b[A-Z]{4}\b", question.upper())
    subject = ticker_match.group(0) if ticker_match else "emiten ini"
    return (
        f"Belum ada evidence yang dapat digunakan untuk menganalisis {subject} "
        "pada corpus saat ini. Sistem tidak menemukan berita relevan maupun "
        "laporan keuangan yang berhasil diproses untuk emiten ini.\n\n"
        "**Cakupan sumber**\n"
        "- Berita: belum tersedia\n"
        "- Laporan Keuangan IDX: belum tersedia"
    )


def coverage_instruction(sources: list[dict[str, str]]) -> str:
    coverage = source_coverage(sources)
    if coverage["news"] and coverage["financial"]:
        return (
            "Berita dan laporan keuangan tersedia. Hubungkan kedua jenis evidence "
            "dan prioritaskan relasi sebab-akibat yang didukung context."
        )
    if coverage["financial"]:
        return (
            "Hanya laporan keuangan yang tersedia. Tetap berikan analisis fundamental "
            "berdasarkan periode terbaru, lalu jelaskan bahwa aspek berita/sentimen terbatas."
        )
    if coverage["news"]:
        return (
            "Hanya berita yang tersedia. Tetap analisis peristiwa dan sentimen, lalu "
            "jelaskan bahwa validasi fundamental terbatas."
        )
    return "Tidak ada evidence tervalidasi; jangan membuat klaim faktual atau sitasi."


__all__ = ["coverage_instruction", "no_evidence_answer", "source_coverage"]
