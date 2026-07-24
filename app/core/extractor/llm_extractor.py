import json
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

from app.core.openai_client import chat_completion
from app.services.crawler.news_crawler import Article
from app.services.crawler.financial_fetcher import FundamentalData

load_dotenv()

EntityType = Literal["PERSON", "ORGANIZATION", "POLICY", "STOCK", "EVENT"]
RelationType = Literal[
    "MANAGES", "ISSUED_BY", "AFFECTS", "REPORTS_TO", "COMPETES_WITH", "PART_OF"
]

_SYSTEM_PROMPT = """\
Kamu adalah sistem ekstraksi informasi untuk analisis pasar saham Indonesia (BEI).

Tugasmu: ekstrak entitas dan relasi dari teks berikut ke dalam format JSON.

## Tipe Entitas
- PERSON     : nama orang (direktur, komisaris, pejabat, analis)
- ORGANIZATION: nama perusahaan, bank, lembaga (OJK, BI, Kemenkeu, emiten BEI)
- POLICY     : kebijakan, regulasi, program pemerintah (contoh: "BI Rate", "GWM", "POJK 12")
- STOCK      : kode atau nama saham BEI (contoh: "BBCA", "Bank Central Asia")
- EVENT      : peristiwa korporasi/ekonomi (RUPS, akuisisi, IPO, right issue, krisis)

## Tipe Relasi
- MANAGES        : PERSON → ORGANIZATION (menjabat sebagai direktur/komisaris)
- ISSUED_BY      : POLICY → ORGANIZATION (kebijakan diterbitkan oleh lembaga)
- AFFECTS        : EVENT/POLICY → STOCK/ORGANIZATION (berdampak pada)
- REPORTS_TO     : ORGANIZATION → ORGANIZATION (anak perusahaan/di bawah pengawasan)
- COMPETES_WITH  : STOCK ↔ STOCK (bersaing di segmen yang sama)
- PART_OF        : PERSON/ORGANIZATION → ORGANIZATION (anggota/bagian dari)

## Format Output JSON (wajib):
{
  "entities": [
    {
      "id": "slug_unik_tanpa_spasi",
      "name": "nama asli dari teks",
      "type": "PERSON|ORGANIZATION|POLICY|STOCK|EVENT",
      "attributes": {"jabatan": "...", "sektor": "...", "keterangan": "..."}
    }
  ],
  "relations": [
    {
      "source": "entity_id",
      "target": "entity_id",
      "type": "MANAGES|ISSUED_BY|AFFECTS|REPORTS_TO|COMPETES_WITH|PART_OF",
      "description": "keterangan singkat konteks relasi"
    }
  ]
}

Aturan:
- id harus lowercase, gunakan underscore sebagai pemisah (contoh: "perry_warjiyo", "bbca")
- Jika entitas tidak jelas tipe-nya, pilih yang paling tepat
- Jangan tambahkan entitas atau relasi yang tidak ada dalam teks
- Hanya kembalikan JSON, tanpa teks tambahan
"""


@dataclass
class Entity:
    id: str
    name: str
    type: EntityType
    attributes: dict = field(default_factory=dict)


@dataclass
class Relation:
    source: str
    target: str
    type: RelationType
    description: str = ""


@dataclass
class ExtractionResult:
    stock_code: str
    source_type: str   # "news" | "financial"
    source_ref: str    # article URL or "financial_report"
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    raw_response: str = ""


def _call_llm(text: str, max_chars: int = 3000) -> dict:
    """Kirim teks ke GPT-4o, kembalikan dict {entities, relations}."""
    truncated = text[:max_chars]

    response = chat_completion(
        caller="app.core.extractor.llm_extractor._call_llm",
        purpose="Entity and Relation Extraction",
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": truncated},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return {"entities": [], "relations": []}, raw


def _parse_result(raw_dict: dict, stock_code: str, source_type: str, source_ref: str, raw: str) -> ExtractionResult:
    entities = [
        Entity(
            id=e.get("id", ""),
            name=e.get("name", ""),
            type=e.get("type", "ORGANIZATION"),
            attributes=e.get("attributes", {}),
        )
        for e in raw_dict.get("entities", [])
    ]
    relations = [
        Relation(
            source=r.get("source", ""),
            target=r.get("target", ""),
            type=r.get("type", "AFFECTS"),
            description=r.get("description", ""),
        )
        for r in raw_dict.get("relations", [])
    ]
    return ExtractionResult(
        stock_code=stock_code,
        source_type=source_type,
        source_ref=source_ref,
        entities=entities,
        relations=relations,
        raw_response=raw,
    )


def extract_from_article(article: Article) -> ExtractionResult:
    """Ekstrak entitas & relasi dari satu artikel berita."""
    text = f"Judul: {article.title}\n\n{article.text}"
    raw_dict, raw = _call_llm(text)
    result = _parse_result(raw_dict, article.stock_code, "news", article.url, raw)
    print(
        f"[{article.stock_code}] news '{article.title[:50]}' → "
        f"{len(result.entities)} entitas, {len(result.relations)} relasi"
    )
    return result


def extract_from_financial(data: FundamentalData) -> ExtractionResult:
    """Ekstrak entitas & relasi dari ringkasan data fundamental emiten."""
    lines = [
        f"Emiten: {data.company_name} ({data.stock_code})",
        f"Tahun laporan: {data.year}",
    ]
    if data.net_profit:
        lines.append(f"Laba bersih: Rp{data.net_profit/1e12:.2f} triliun")
    if data.revenue:
        lines.append(f"Pendapatan: Rp{data.revenue/1e12:.2f} triliun")
    if data.eps:
        lines.append(f"EPS: {data.eps:.0f}")
    if data.roe:
        lines.append(f"ROE: {data.roe:.1%}")
    if data.per:
        lines.append(f"PER: {data.per:.1f}x")
    if data.total_assets:
        lines.append(f"Total aset: Rp{data.total_assets/1e12:.2f} triliun")
    if data.total_equity:
        lines.append(f"Total ekuitas: Rp{data.total_equity/1e12:.2f} triliun")
    if data.raw_text:
        lines.append("\n--- Kutipan laporan keuangan ---")
        lines.append(data.raw_text[:1500])

    text = "\n".join(lines)
    raw_dict, raw = _call_llm(text)
    result = _parse_result(raw_dict, data.stock_code, "financial", "financial_report", raw)
    print(
        f"[{data.stock_code}] financial → "
        f"{len(result.entities)} entitas, {len(result.relations)} relasi"
    )
    return result


def extract_all(
    articles: dict[str, list[Article]],
    financial_data: dict[str, FundamentalData],
) -> dict[str, list[ExtractionResult]]:
    """
    Jalankan ekstraksi untuk semua artikel dan data fundamental.

    Args:
        articles      : output dari news_crawler.crawl_news()
        financial_data: output dari financial_fetcher.fetch_multiple()

    Returns:
        dict {kode_saham: [ExtractionResult, ...]}
    """
    all_results: dict[str, list[ExtractionResult]] = {}

    # Ekstraksi dari berita
    for code, art_list in articles.items():
        results = all_results.setdefault(code, [])
        for article in art_list:
            try:
                results.append(extract_from_article(article))
            except Exception as exc:
                print(f"[{code}] ERROR artikel '{article.title[:40]}': {exc}")

    # Ekstraksi dari data fundamental
    for code, fund_data in financial_data.items():
        results = all_results.setdefault(code, [])
        try:
            results.append(extract_from_financial(fund_data))
        except Exception as exc:
            print(f"[{code}] ERROR financial: {exc}")

    return all_results


def extract_search_keywords(stock_code: str, question: str, n: int = 3) -> list[str]:
    """Gunakan LLM untuk menghasilkan query pencarian berita dari pertanyaan user.

    Args:
        stock_code: kode saham target, contoh "BBCA"
        question  : pertanyaan user dalam Bahasa Indonesia
        n         : jumlah query yang dihasilkan (default 3)

    Returns:
        list string query untuk dipakai di crawl_by_keywords()
    """
    prompt = (
        f"Buat {n} query pencarian berita Google News yang spesifik dan relevan "
        f"untuk menjawab pertanyaan berikut tentang saham Indonesia.\n\n"
        f"Kode saham : {stock_code}\n"
        f"Pertanyaan : {question}\n\n"
        f"Aturan:\n"
        f"- Query dalam Bahasa Indonesia atau campuran (sesuai konteks)\n"
        f"- Spesifik ke topik pertanyaan, bukan query umum\n"
        f"- Setiap query berbeda sudut pandang\n\n"
        f"Kembalikan JSON: {{\"queries\": [\"...\", \"...\", \"...\"]}}"
    )
    try:
        resp = chat_completion(
            caller="app.core.extractor.llm_extractor.extract_search_keywords",
            purpose="News Search Query Rewrite",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=200,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        queries = [q for q in parsed.get("queries", []) if isinstance(q, str) and q.strip()]
        print(f"[{stock_code}] Keywords: {queries}")
        return queries[:n] if queries else [f"{stock_code} saham"]
    except Exception as exc:
        print(f"[{stock_code}] keyword extraction ERROR: {exc}")
        return [f"{stock_code} saham"]
