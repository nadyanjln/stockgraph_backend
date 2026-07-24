import json
from dataclasses import dataclass

from dotenv import load_dotenv

from app.core.openai_client import chat_completion
from app.core.extractor.llm_extractor import ExtractionResult

load_dotenv()

_JUDGE_SYSTEM = """\
Kamu adalah hakim relevansi (relevance judge) untuk sistem analisis saham Indonesia.

Tugasmu: nilai apakah suatu konten benar-benar relevan dan bernilai \
untuk analisis investasi pada emiten target.

Kembalikan HANYA JSON dengan format:
{
  "relevance": <float 0.0–1.0>,
  "confidence": <float 0.0–1.0>,
  "reason": "<penjelasan singkat 1–2 kalimat>"
}

Panduan skor relevansi:
- 0.8–1.0 : Sangat relevan — konten utama membahas emiten target, ada info keuangan/bisnis/korporasi
- 0.6–0.8 : Cukup relevan — mayoritas berkaitan, meski ada bagian yang tidak langsung terkait
- 0.4–0.6 : Sebagian relevan — menyebut emiten target tapi bukan fokus utama artikel
- 0.0–0.4 : Tidak relevan — emiten hanya disebut selintas, konten tidak berguna untuk analisis
"""

_JUDGE_TEMPLATE = """\
## Emiten Target
Kode saham : {stock_code}
Nama       : {company_name}

## Sumber Konten
Tipe  : {source_type}
Ref   : {source_ref}

## Entitas yang Diekstrak
{entities_text}

## Relasi yang Diekstrak
{relations_text}

Nilailah seberapa relevan konten ini untuk analisis investasi pada {stock_code}.
"""


@dataclass
class RelevanceScore:
    stock_code: str
    source_ref: str
    relevance: float
    confidence: float
    reason: str
    passed: bool


@dataclass
class CheckedResult:
    extraction: ExtractionResult
    score: RelevanceScore


def _format_entities(extraction: ExtractionResult) -> str:
    if not extraction.entities:
        return "(tidak ada entitas)"
    return "\n".join(
        f"- [{e.type}] {e.name}" + (f" — {e.attributes}" if e.attributes else "")
        for e in extraction.entities
    )


def _format_relations(extraction: ExtractionResult) -> str:
    if not extraction.relations:
        return "(tidak ada relasi)"
    return "\n".join(
        f"- {r.source} --[{r.type}]--> {r.target}: {r.description}"
        for r in extraction.relations
    )


def check_relevance(
    extraction: ExtractionResult,
    company_name: str = "",
    threshold: float = 0.6,
    model: str = "gpt-4o-mini",
) -> RelevanceScore:
    """
    Nilai relevansi satu ExtractionResult terhadap emiten target.

    Args:
        extraction  : hasil ekstraksi dari llm_extractor
        company_name: nama lengkap emiten (opsional, untuk konteks prompt)
        threshold   : batas minimum skor untuk dianggap lolos (default 0.6)
        model       : model LLM yang digunakan (default gpt-4o-mini)

    Returns:
        RelevanceScore dengan field passed=True jika relevance >= threshold
    """
    user_prompt = _JUDGE_TEMPLATE.format(
        stock_code=extraction.stock_code,
        company_name=company_name or extraction.stock_code,
        source_type=extraction.source_type,
        source_ref=extraction.source_ref[:120],
        entities_text=_format_entities(extraction),
        relations_text=_format_relations(extraction),
    )

    response = chat_completion(
        caller="app.core.extractor.relevance_checker.check_relevance",
        purpose="Evidence Relevance Judge",
        model=model,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}

    relevance = float(parsed.get("relevance", 0.0))
    confidence = float(parsed.get("confidence", 0.0))
    reason = parsed.get("reason", "")

    score = RelevanceScore(
        stock_code=extraction.stock_code,
        source_ref=extraction.source_ref,
        relevance=relevance,
        confidence=confidence,
        reason=reason,
        passed=relevance >= threshold,
    )

    status = "LOLOS" if score.passed else "DITOLAK"
    print(
        f"[{extraction.stock_code}] {status} "
        f"({extraction.source_type}) rel={relevance:.2f} conf={confidence:.2f} — {reason}"
    )
    return score


def filter_results(
    results: dict[str, list[ExtractionResult]],
    company_names: dict[str, str] | None = None,
    threshold: float = 0.6,
    model: str = "gpt-4o-mini",
) -> dict[str, list[CheckedResult]]:
    """
    Jalankan relevance check pada semua ExtractionResult, kembalikan semua
    beserta skor (gunakan .passed untuk filter yang lolos).

    Args:
        results      : output dari llm_extractor.extract_all()
        company_names: mapping {kode_saham: nama_perusahaan} untuk konteks prompt
        threshold    : batas minimum skor relevansi (default 0.6)
        model        : model LLM yang digunakan (default gpt-4o-mini)

    Returns:
        dict {kode_saham: [CheckedResult, ...]}  — semua hasil termasuk yang ditolak
    """
    company_names = company_names or {}
    checked: dict[str, list[CheckedResult]] = {}

    for code, extractions in results.items():
        checked[code] = []
        for ext in extractions:
            try:
                score = check_relevance(
                    ext,
                    company_name=company_names.get(code, ""),
                    threshold=threshold,
                    model=model,
                )
                checked[code].append(CheckedResult(extraction=ext, score=score))
            except Exception as exc:
                print(f"[{code}] ERROR relevance check: {exc}")

    return checked


def get_passed(checked: dict[str, list[CheckedResult]]) -> dict[str, list[CheckedResult]]:
    """Helper: filter hanya CheckedResult yang lolos threshold."""
    return {
        code: [r for r in items if r.score.passed]
        for code, items in checked.items()
    }
