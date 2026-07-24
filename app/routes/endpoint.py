"""General API endpoints for StockGraph."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.agent.response_formatter import format_rag_response
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.core.extractor.idx_statement_extractor import (
    KeyFinancialsData,
    extract_key_financials,
)
from app.services.database.graph_builder import (
    explore_provenance_graph,
    list_year_graphs,
    validate_graph,
)
from app.services.database.evidence_retriever import retrieve_local_evidence

router = APIRouter(prefix="/api", tags=["endpoint"])

# In-memory cache untuk Key Financials (TTL 1 jam) — hindari refetch PDF + LLM
# setiap kali ResultView mount.
_KEY_FIN_CACHE: dict[str, tuple[float, KeyFinancialsData]] = {}
_KEY_FIN_TTL_SECONDS = 60 * 60


class QueryRequest(BaseModel):
    question: str
    year: int | None = None


@router.get("/health")
async def health(request: Request):
    engine = getattr(request.app.state, "engine", None)
    return {
        "status": "ok",
        "engine_ready": engine is not None,
        "years": engine.available_years if engine else list_year_graphs(),
    }


@router.get("/years")
async def get_years():
    years = list_year_graphs()
    return {"years": years, "default": years[-1] if years else None}


@router.get("/validate/{year}")
async def get_validate(year: int):
    return validate_graph(year)


@router.get("/debug/retrieval")
async def debug_retrieval(
    question: str,
    year: int | None = None,
    _: User = Depends(get_current_user),
):
    """Developer diagnostics only; no chain-of-thought is exposed."""
    bundle = retrieve_local_evidence(question, target_year=year, max_hops=2)
    return {
        "diagnostics": bundle.diagnostics.as_dict() if bundle.diagnostics else {},
        "news_sources": bundle.news_sources,
        "financial_sources": bundle.financial_sources,
        "graph_paths": bundle.graph_paths,
    }


@router.get("/graph/explore")
async def explore_graph(
    stock_codes: str = "",
    year: int | None = None,
    node_id: str | None = None,
    depth: int = Query(default=1, ge=0, le=3),
    limit: int = Query(default=120, ge=10, le=300),
    _: User = Depends(get_current_user),
):
    codes = [code for code in stock_codes.split(",") if code.strip()]
    return explore_provenance_graph(
        stock_codes=codes,
        year=year,
        node_id=node_id,
        depth=depth,
        limit=limit,
    )


@router.post("/query")
async def query_graph(
    req: QueryRequest,
    request: Request,
    _: User = Depends(get_current_user),
):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return {"error": "GraphRAG engine is not initialized"}
    result = await engine.query(req.question, year=req.year, return_context=True)
    formatted = format_rag_response(result.answer, result.citations, result.sources)
    return {
        "question": result.question,
        "answer": result.answer,
        **formatted,
        "year": result.year,
        "citations": result.citations,
        "context": result.context,
    }


@router.get("/key-financials/{stock_code}")
async def get_key_financials(
    stock_code: str,
    use_llm: bool = True,
    _: User = Depends(get_current_user),
):
    """
    Ekstrak tabel Key Financials emiten BEI: 3 tahun historical + TTM
    (Revenue, Net Income, EPS, ROE, Debt to Equity).

    Sumber: yfinance + PDF IDX (LLM-enriched bila `use_llm=True`).
    Response sudah berformat siap pakai untuk frontend (kolom & baris).
    """
    code = stock_code.upper().strip()
    if not code:
        raise HTTPException(status_code=400, detail="stock_code is required")

    cache_key = f"{code}:{int(use_llm)}"
    now = time.time()
    cached = _KEY_FIN_CACHE.get(cache_key)
    if cached and now - cached[0] < _KEY_FIN_TTL_SECONDS:
        data = cached[1]
    else:
        try:
            data = await asyncio.to_thread(extract_key_financials, code, use_llm)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"failed to extract key financials: {type(exc).__name__}: {exc}",
            )
        _KEY_FIN_CACHE[cache_key] = (now, data)

    return {
        "stock_code": data.stock_code,
        "company_name": data.company_name,
        "source": data.source,
        "generated_at": data.generated_at,
        "columns": [
            {"label": col.label, "period": col.period, "year": col.year}
            for col in data.columns
        ],
        "rows": [
            {"label": row.label, "values": row.values, "formatted": row.formatted}
            for row in data.rows
        ],
    }
