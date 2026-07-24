"""
IDX Financial Statement Extractor Agent.

Ekstraksi laporan keuangan IDX (tahunan + TTM) jadi tabel siap pakai untuk
frontend Key Financials. Sumber:
  1. yfinance (3-tahun annual + 4-kuartal terkini untuk TTM)
  2. PDF tahunan IDX (di-enrich pakai LLM untuk akurasi angka kunci)

Output: KeyFinancialsData dengan 4 kolom (3 historical years + TTM) × 5 metrik
(Revenue, Net Income, EPS, ROE, Debt to Equity).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from dotenv import load_dotenv

from app.core.openai_client import chat_completion
from app.services.crawler.financial_fetcher import (
    FundamentalData,
    YearlySnapshot,
    fetch_from_yfinance,
    parse_pdf,
    _try_download_idx_pdf,
)

load_dotenv()

EXTRACTOR_MODEL = os.getenv("IDX_EXTRACTOR_MODEL", "gpt-4o-mini")

# Field metrik yang LLM diminta isi dari teks PDF
_METRIC_KEYS = ("revenue", "net_profit", "total_equity", "total_debt", "eps")

_SYSTEM_PROMPT = """\
Kamu adalah extractor laporan keuangan emiten Bursa Efek Indonesia (BEI).

Tugas: ekstrak 5 angka kunci dari teks laporan keuangan tahunan (PDF IDX) yang
diberikan, lalu kembalikan JSON dengan format pasti berikut:

{
  "revenue": <number | null>,           // pendapatan / total revenue (dalam JUTA rupiah)
  "net_profit": <number | null>,        // laba bersih (dalam JUTA rupiah)
  "total_equity": <number | null>,      // total ekuitas (dalam JUTA rupiah)
  "total_debt": <number | null>,        // total liabilitas berbunga / total debt (dalam JUTA rupiah)
  "eps": <number | null>                // laba per saham dasar (dalam RUPIAH per lembar)
}

Aturan:
- Semua angka dalam JUTA rupiah (1.000.000) kecuali EPS dalam rupiah per lembar.
- Jika laporan menyatakan satuan triliun, konversi ke juta (× 1.000.000).
- Jika satuan ribuan, konversi ke juta (÷ 1.000).
- Kalau angka tidak ditemukan, isi `null` (jangan menebak).
- Untuk bank: revenue = pendapatan bunga bersih (Net Interest Income).
- Hanya kembalikan JSON valid, tanpa teks lain.
"""

Period = Literal["FY", "TTM"]


@dataclass
class KeyFinancialRow:
    """Satu metrik untuk semua kolom (3 tahun + TTM)."""
    label: str
    values: list[float | None] = field(default_factory=list)
    formatted: list[str] = field(default_factory=list)


@dataclass
class KeyFinancialColumn:
    """Header kolom (tahun atau TTM)."""
    label: str   # "2022" | "2023" | "2024" | "TTM"
    period: Period
    year: int    # untuk TTM = tahun terkini


@dataclass
class KeyFinancialsData:
    stock_code: str
    company_name: str = ""
    columns: list[KeyFinancialColumn] = field(default_factory=list)
    rows: list[KeyFinancialRow] = field(default_factory=list)
    source: str = "yfinance"
    generated_at: str = ""


# ── Formatter ─────────────────────────────────────────────────────────────────

def _fmt_money(value: float | None) -> str:
    """Format angka uang (dalam juta rupiah) jadi string ringkas, mis. 82,001."""
    if value is None:
        return "—"
    millions = value / 1_000_000
    return f"{millions:,.0f}"


def _fmt_eps(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


# ── LLM extraction ────────────────────────────────────────────────────────────

def _llm_extract_metrics(text: str, stock_code: str, year: int) -> dict[str, float | None]:
    """Minta GPT mengekstrak 5 metrik kunci dari teks laporan IDX."""
    if not text.strip():
        return {k: None for k in _METRIC_KEYS}

    truncated = text[:12000]

    user_prompt = (
        f"Emiten: {stock_code}\n"
        f"Tahun laporan: {year}\n\n"
        f"Teks laporan keuangan:\n{truncated}"
    )

    try:
        response = chat_completion(
            caller="app.core.extractor.idx_statement_extractor._llm_extract_metrics",
            purpose="IDX Financial Metric Extraction",
            model=EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        print(f"[idx_extractor] LLM error {stock_code}/{year}: {exc}")
        return {k: None for k in _METRIC_KEYS}

    parsed: dict[str, float | None] = {}
    for key in _METRIC_KEYS:
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            # Konversi juta → unit dasar (rupiah) agar konsisten dengan yfinance.
            parsed[key] = val * 1_000_000 if key != "eps" else float(val)
        else:
            parsed[key] = None
    return parsed


def _merge_snapshot_with_llm(snap: YearlySnapshot, stock_code: str) -> YearlySnapshot:
    """Override metrik snapshot pakai hasil LLM dari PDF (jika tersedia)."""
    pdf_bytes = _try_download_idx_pdf(stock_code, snap.year)
    if not pdf_bytes:
        return snap

    try:
        metrics = parse_pdf(
            pdf_bytes,
            source_file=f"idx_cdn_{stock_code}_{snap.year}_Tahunan.pdf",
            document_year=snap.year,
            ticker=stock_code,
        )
    except Exception as exc:
        print(f"[idx_extractor] PDF parse error {stock_code}/{snap.year}: {exc}")
        return snap

    raw_text = metrics.get("raw_text", "")
    llm_values = _llm_extract_metrics(raw_text, stock_code, snap.year)

    if llm_values.get("revenue") is not None:
        snap.revenue = llm_values["revenue"]
    if llm_values.get("net_profit") is not None:
        snap.net_profit = llm_values["net_profit"]
    if llm_values.get("total_equity") is not None:
        snap.total_equity = llm_values["total_equity"]
    if llm_values.get("total_debt") is not None:
        snap.total_debt = llm_values["total_debt"]
    if llm_values.get("eps") is not None:
        snap.eps = llm_values["eps"]
    snap.pdf_path = f"idx_cdn_{stock_code}_{snap.year}_Tahunan"
    return snap


# ── TTM aggregation ───────────────────────────────────────────────────────────

def _compute_ttm(data: FundamentalData) -> dict[str, float | None]:
    """Hitung TTM dari 4 quarter terakhir + data balance sheet terkini."""
    quarterly = data.quarterly[:4] if data.quarterly else []

    revenue_parts = [q.revenue for q in quarterly if q.revenue]
    netprofit_parts = [q.net_profit for q in quarterly if q.net_profit]

    return {
        "revenue": sum(revenue_parts) if len(revenue_parts) == 4 else data.revenue,
        "net_profit": sum(netprofit_parts) if len(netprofit_parts) == 4 else data.net_profit,
        "eps": data.eps,                # trailingEps dari yfinance.info sudah TTM
        "total_equity": data.total_equity,
        "total_debt": data.total_debt,
        "roe": data.roe,                # info.returnOnEquity sudah TTM
    }


# ── Derived metrics ───────────────────────────────────────────────────────────

def _roe(net_profit: float | None, total_equity: float | None) -> float | None:
    if net_profit is None or not total_equity:
        return None
    return net_profit / total_equity


def _der(total_debt: float | None, total_equity: float | None) -> float | None:
    if total_debt is None or not total_equity:
        return None
    return total_debt / total_equity


# ── Public API ────────────────────────────────────────────────────────────────

def extract_key_financials(
    stock_code: str,
    use_llm: bool = True,
    max_years: int = 3,
) -> KeyFinancialsData:
    """
    Ekstrak Key Financials untuk satu emiten BEI: 3 tahun historical + TTM.

    Args:
        stock_code: kode emiten IDX (mis. "BBCA")
        use_llm:    enrich tiap tahun dengan LLM dari PDF IDX (lebih akurat tapi pelan)
        max_years:  jumlah tahun historical (default 3)

    Returns:
        KeyFinancialsData siap pakai untuk frontend table.
    """
    fundamental = fetch_from_yfinance(stock_code)
    historical = fundamental.historical[:max_years]

    if use_llm:
        historical = [
            _merge_snapshot_with_llm(snap, stock_code) for snap in historical if snap.year > 0
        ]

    historical = sorted(historical, key=lambda s: s.year)
    ttm = _compute_ttm(fundamental)

    columns: list[KeyFinancialColumn] = [
        KeyFinancialColumn(label=str(snap.year), period="FY", year=snap.year)
        for snap in historical
    ]
    latest_year = historical[-1].year if historical else datetime.now().year
    columns.append(KeyFinancialColumn(label="TTM", period="TTM", year=latest_year))

    rev_vals = [s.revenue for s in historical] + [ttm["revenue"]]
    ni_vals = [s.net_profit for s in historical] + [ttm["net_profit"]]
    eps_vals = [s.eps for s in historical] + [ttm["eps"]]
    roe_vals = [_roe(s.net_profit, s.total_equity) for s in historical] + [ttm["roe"]]
    der_vals = [_der(s.total_debt, s.total_equity) for s in historical] + [
        _der(ttm["total_debt"], ttm["total_equity"])
    ]

    rows = [
        KeyFinancialRow(
            label="Revenue",
            values=rev_vals,
            formatted=[_fmt_money(v) for v in rev_vals],
        ),
        KeyFinancialRow(
            label="Net Income",
            values=ni_vals,
            formatted=[_fmt_money(v) for v in ni_vals],
        ),
        KeyFinancialRow(
            label="EPS (IDR)",
            values=eps_vals,
            formatted=[_fmt_eps(v) for v in eps_vals],
        ),
        KeyFinancialRow(
            label="ROE",
            values=roe_vals,
            formatted=[_fmt_pct(v) for v in roe_vals],
        ),
        KeyFinancialRow(
            label="Debt to Equity",
            values=der_vals,
            formatted=[_fmt_ratio(v) for v in der_vals],
        ),
    ]

    return KeyFinancialsData(
        stock_code=stock_code,
        company_name=fundamental.company_name,
        columns=columns,
        rows=rows,
        source="yfinance+idx_pdf+llm" if use_llm else "yfinance",
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )


__all__ = [
    "KeyFinancialRow",
    "KeyFinancialColumn",
    "KeyFinancialsData",
    "extract_key_financials",
]
