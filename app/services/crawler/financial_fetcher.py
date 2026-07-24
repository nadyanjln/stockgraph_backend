"""
Financial Fetcher Agent — fetch fundamental data emiten BEI.

Sumber data:
  1. yfinance (primary) — historical 3 tahun + kuartalan terkini
  2. IDX PDF (opsional) — laporan resmi untuk validasi tahunan

Output: FundamentalData dengan list `historical` per tahun (untuk multi-tenant graph
per tahun) dan ringkasan terkini.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import requests
import yfinance as yf

from app.services.pdf_extractor import combine_page_text, extract_pdf_pages

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537",
    "Accept-Language": "id-ID,id;q=0.9",
}

IDX_PDF_PATTERN = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies"
    "/Financial_Reports/{year}/{period}/{code}_{period}_{year}.pdf"
)

HISTORY_YEARS = 3


@dataclass
class PeriodSnapshot:
    """Snapshot kuartalan untuk tracking tren intra-year."""
    date: str
    net_profit: float | None = None
    revenue: float | None = None
    net_interest_income: float | None = None


@dataclass
class YearlySnapshot:
    """Snapshot fundamental satu tahun fiskal — unit utama multi-tenant graph."""
    year: int
    revenue: float | None = None
    net_interest_income: float | None = None
    net_profit: float | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    total_debt: float | None = None
    operating_cash_flow: float | None = None
    eps: float | None = None
    pdf_path: str = ""
    raw_text: str = ""


@dataclass
class FundamentalData:
    """Data fundamental terkini + 3-tahun historical untuk satu emiten BEI."""
    stock_code: str
    company_name: str = ""

    # Tahun terkini (untuk backward-compat dengan llm_extractor)
    year: int = 0
    revenue: float | None = None
    net_interest_income: float | None = None
    net_profit: float | None = None
    eps: float | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    total_debt: float | None = None
    operating_cash_flow: float | None = None

    # Rasio valuasi (terkini, dari yfinance.info)
    per: float | None = None
    pbv: float | None = None
    roe: float | None = None
    roa: float | None = None
    profit_margin: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None

    # Historical 3 tahun (untuk per-year graph multi-tenant)
    historical: list[YearlySnapshot] = field(default_factory=list)

    # 4 snapshot kuartalan terakhir
    quarterly: list[PeriodSnapshot] = field(default_factory=list)

    source: str = "yfinance"
    pdf_path: str = ""
    raw_text: str = ""


def _safe_get(df, row: str, col_idx: int = 0) -> float | None:
    """Ambil nilai dari kolom ke-`col_idx` baris `row`; None jika tidak ada/NaN."""
    if df.empty or row not in df.index or col_idx >= len(df.columns):
        return None
    try:
        v = float(df.iloc[df.index.get_loc(row), col_idx])
        return None if (v != v) else v
    except (TypeError, ValueError, IndexError):
        return None


def _extract_yearly_snapshots(
    income_stmt, balance_sheet, cashflow, max_years: int = HISTORY_YEARS,
) -> list[YearlySnapshot]:
    """Bangun list YearlySnapshot dari DataFrame yfinance (annual)."""
    if income_stmt.empty:
        return []

    snapshots: list[YearlySnapshot] = []
    n_cols = min(max_years, len(income_stmt.columns))
    for i in range(n_cols):
        col = income_stmt.columns[i]
        year = col.year if hasattr(col, "year") else 0

        snapshots.append(YearlySnapshot(
            year=year,
            revenue=_safe_get(income_stmt, "Total Revenue", i),
            net_interest_income=_safe_get(income_stmt, "Net Interest Income", i),
            net_profit=_safe_get(income_stmt, "Net Income", i),
            total_assets=_safe_get(balance_sheet, "Total Assets", i),
            total_equity=_safe_get(balance_sheet, "Stockholders Equity", i),
            total_debt=_safe_get(balance_sheet, "Total Debt", i),
            operating_cash_flow=_safe_get(cashflow, "Operating Cash Flow", i),
            eps=_safe_get(income_stmt, "Basic EPS", i),
        ))
    return snapshots


def fetch_from_yfinance(stock_code: str) -> FundamentalData:
    """Ambil data fundamental + historical 3 tahun dari Yahoo Finance (.JK)."""
    ticker = yf.Ticker(f"{stock_code}.JK")
    info = ticker.info

    income = ticker.income_stmt
    balance = ticker.balance_sheet
    cashflow = ticker.cashflow
    quarterly_income = ticker.quarterly_income_stmt

    historical = _extract_yearly_snapshots(income, balance, cashflow)
    latest = historical[0] if historical else YearlySnapshot(year=0)

    quarterly: list[PeriodSnapshot] = []
    if not quarterly_income.empty:
        for i, col in enumerate(quarterly_income.columns[:4]):
            quarterly.append(PeriodSnapshot(
                date=col.strftime("%Y-%m-%d"),
                net_profit=_safe_get(quarterly_income, "Net Income", i),
                revenue=_safe_get(quarterly_income, "Total Revenue", i),
                net_interest_income=_safe_get(quarterly_income, "Net Interest Income", i),
            ))

    return FundamentalData(
        stock_code=stock_code,
        company_name=info.get("shortName", ""),
        year=latest.year,
        revenue=latest.revenue,
        net_interest_income=latest.net_interest_income,
        net_profit=latest.net_profit,
        eps=info.get("trailingEps") or latest.eps,
        total_assets=latest.total_assets,
        total_equity=latest.total_equity,
        total_debt=latest.total_debt,
        operating_cash_flow=latest.operating_cash_flow,
        per=info.get("trailingPE"),
        pbv=info.get("priceToBook"),
        roe=info.get("returnOnEquity"),
        roa=info.get("returnOnAssets"),
        profit_margin=info.get("profitMargins"),
        revenue_growth=info.get("revenueGrowth"),
        earnings_growth=info.get("earningsGrowth"),
        historical=historical,
        quarterly=quarterly,
        source="yfinance",
    )


def _try_download_idx_pdf(stock_code: str, year: int, period: str = "Tahunan") -> bytes | None:
    """Coba download PDF laporan keuangan dari CDN IDX."""
    url = IDX_PDF_PATTERN.format(year=year, period=period, code=stock_code)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 200 and "pdf" in resp.headers.get("Content-Type", "").lower():
            print(f"[{stock_code}] PDF IDX {year} OK: {url}")
            return resp.content
    except requests.RequestException:
        pass
    return None


def parse_pdf(
    pdf_source: str | bytes,
    *,
    source_file: str = "",
    document_year: int | None = None,
    ticker: str = "",
    company: str = "",
) -> dict:
    """Parse laporan keuangan PDF using PaddleOCR document extraction."""
    extraction = extract_pdf_pages(
        pdf_source,
        source_file=source_file,
        document_year=document_year,
        ticker=ticker,
        company=company,
    )
    full_text = combine_page_text(extraction.pages)
    tables = [
        table
        for page in extraction.pages
        for table in page.tables
    ]

    metrics = _parse_metrics_from_text(full_text)
    metrics["tables_count"] = len(tables)
    metrics["tables"] = tables[:15]
    metrics["raw_text"] = full_text
    metrics["source_file"] = extraction.source_file
    metrics["metrics_extraction_method"] = "paddleocr"
    metrics["page_extractions"] = [page.to_dict() for page in extraction.pages]
    metrics["native_text_length"] = sum(
        len(page.text.strip())
        for page in extraction.pages
    )
    metrics["needs_review_pages"] = [
        page.page_number for page in extraction.pages if page.needs_review
    ]
    metrics["extraction_errors"] = [
        {
            "source_file": page.source_file,
            "page_number": page.page_number,
            "extraction_method": page.extraction_method,
            "error": page.error,
        }
        for page in extraction.pages
        if page.error
    ]
    if extraction.error:
        metrics["extraction_errors"].append({
            "source_file": extraction.source_file,
            "page_number": None,
            "extraction_method": "paddleocr",
            "error": extraction.error,
        })
    return metrics


def _parse_idr_number(text: str) -> float | None:
    """Parse format laporan keuangan Indonesia: 1.234.567 atau (1.234) untuk negatif."""
    if not text:
        return None
    s = re.sub(r"[Rp\s]", "", text.strip())
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return None


def _parse_metrics_from_text(text: str) -> dict:
    """Ekstrak metrik kunci dari teks laporan IDX (BI & Inggris)."""
    patterns: dict[str, list[str]] = {
        "net_profit": [
            r"(?:laba(?:\s+bersih)?(?:\s+tahun berjalan)?|laba komprehensif)\s*[:\-]?\s*([\d.,]+)",
            r"(?:net profit|profit for the year)\s*[:\-]?\s*([\d.,]+)",
        ],
        "total_assets": [
            r"(?:jumlah aset|total aset)\s*[:\-]?\s*([\d.,]+)",
            r"total assets\s*[:\-]?\s*([\d.,]+)",
        ],
        "total_equity": [
            r"(?:jumlah ekuitas|total ekuitas)\s*[:\-]?\s*([\d.,]+)",
            r"total equity\s*[:\-]?\s*([\d.,]+)",
        ],
        "eps": [
            r"laba(?:\s+bersih)?\s+per\s+saham(?:\s+dasar)?\s*[:\-]?\s*([\d.,]+)",
            r"(?:earnings per share|basic eps)\s*[:\-]?\s*([\d.,]+)",
        ],
        "revenue": [
            r"(?:pendapatan(?:\s+bunga)?(?:\s+bersih)?|penjualan bersih)\s*[:\-]?\s*([\d.,]+)",
            r"(?:total revenue|net revenue)\s*[:\-]?\s*([\d.,]+)",
        ],
    }

    result: dict = {}
    for key, pats in patterns.items():
        for pattern in pats:
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if m:
                val = _parse_idr_number(m.group(1))
                if val is not None and val > 0:
                    result[key] = val
                    break
        if key not in result:
            result[key] = None

    return result


def _enrich_historical_with_idx_pdf(data: FundamentalData) -> None:
    """Untuk setiap snapshot historical, coba download PDF IDX dan override metrik utama."""
    for snap in data.historical:
        if snap.year <= 0:
            continue
        pdf_bytes = _try_download_idx_pdf(data.stock_code, snap.year)
        if not pdf_bytes:
            continue
        try:
            metrics = parse_pdf(
                pdf_bytes,
                source_file=f"idx_cdn_{data.stock_code}_{snap.year}_Tahunan.pdf",
                document_year=snap.year,
                ticker=data.stock_code,
                company=data.company_name,
            )
        except Exception as exc:
            print(f"[{data.stock_code}] PDF parse error tahun {snap.year}: {exc}")
            continue

        for key in ("net_profit", "total_assets", "total_equity", "eps", "revenue"):
            if metrics.get(key):
                setattr(snap, key, metrics[key])
        snap.pdf_path = f"idx_cdn_{data.stock_code}_{snap.year}_Tahunan"
        snap.raw_text = metrics.get("raw_text", "")[:6000]


def fetch_financial_data(
    stock_code: str,
    pdf_path: str | None = None,
    try_idx_pdf: bool = False,
) -> FundamentalData:
    """
    Fetch fundamental data + 3-tahun historical untuk satu emiten BEI.

    Args:
        stock_code:  kode emiten IDX (mis. "BBCA")
        pdf_path:    path lokal ke PDF laporan keuangan
        try_idx_pdf: download PDF IDX otomatis untuk tiap tahun historical

    Returns:
        FundamentalData dengan `historical` list 3 tahun
    """
    data = fetch_from_yfinance(stock_code)
    _log(stock_code, data)

    if pdf_path:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        metrics = parse_pdf(
            pdf_bytes,
            source_file=pdf_path,
            document_year=data.year or None,
            ticker=stock_code,
            company=data.company_name,
        )
        for key in ("net_profit", "total_assets", "total_equity", "eps", "revenue"):
            if metrics.get(key):
                setattr(data, key, metrics[key])
        data.raw_text = metrics.get("raw_text", "")[:8000]
        data.pdf_path = pdf_path
        data.source = "yfinance+pdf_lokal"

    elif try_idx_pdf:
        _enrich_historical_with_idx_pdf(data)
        if any(s.pdf_path for s in data.historical):
            data.source = "yfinance+idx_pdf"

    return data


def fetch_multiple(
    stock_codes: list[str],
    try_idx_pdf: bool = False,
) -> dict[str, FundamentalData]:
    """Fetch fundamental data untuk banyak emiten sekaligus."""
    results: dict[str, FundamentalData] = {}
    for code in stock_codes:
        try:
            results[code] = fetch_financial_data(code, try_idx_pdf=try_idx_pdf)
        except Exception as exc:
            print(f"[{code}] ERROR: {exc}")
    return results


def years_covered(data: FundamentalData) -> list[int]:
    """Daftar tahun valid dari historical (untuk routing multi-tenant)."""
    return [s.year for s in data.historical if s.year > 0]


def current_year() -> int:
    return datetime.now().year


def _log(code: str, data: FundamentalData) -> None:
    parts = [f"[{code}] {data.company_name}"]
    if data.historical:
        years = ", ".join(str(s.year) for s in data.historical)
        parts.append(f"history=[{years}]")
    if data.net_profit:
        parts.append(f"laba_terkini={data.net_profit / 1e12:.2f}T")
    if data.roe:
        parts.append(f"ROE={data.roe:.1%}")
    print(" | ".join(parts))
