"""FastAPI API server for StockGraph.

The interactive application UI is Streamlit:
    uv run streamlit run app/services/ui/streamlit_app.py

Run this server only when API or WebSocket integration is needed:
    uv run uvicorn app.services.ui.server:app --reload --port 8000
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.agent.orchestrator import Orchestrator, SessionStore
from app.core.extractor.llm_extractor import extract_all, extract_search_keywords
from app.core.extractor.relevance_checker import filter_results
from app.services.crawler.financial_fetcher import fetch_multiple
from app.services.crawler.news_crawler import crawl_by_keywords
from app.services.database.graph_builder import (
    build_graph_multi_tenant,
    list_year_graphs,
    validate_graph,
)
from app.services.database.graphrag_engine import GraphRAGEngine
from app.routes.endpoint import router as endpoint_router
from app.routes.merger_routes import router as merger_router

load_dotenv()

# ── Global instances (single-process) ─────────────────────────────────────────

_engine: GraphRAGEngine | None = None
_orchestrator: Orchestrator | None = None
_session_store = SessionStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _orchestrator
    _engine = GraphRAGEngine()
    await _engine.initialize()
    _orchestrator = Orchestrator(_engine, session_store=_session_store)
    app.state.engine = _engine
    app.state.orchestrator = _orchestrator
    app.state.session_store = _session_store
    print(f"[server] ready, years={_engine.available_years}")
    yield
    if _engine:
        await _engine.close()


app = FastAPI(title="StockGraph", lifespan=lifespan)
app.include_router(endpoint_router)
app.include_router(merger_router)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return {
        "name": "StockGraph API",
        "ui": "uv run streamlit run app/services/ui/streamlit_app.py",
        "docs": "/docs",
    }


@app.get("/api/years")
async def get_years():
    years = list_year_graphs()
    return {"years": years, "default": years[-1] if years else None}


@app.get("/api/validate/{year}")
async def get_validate(year: int):
    return validate_graph(year)


# ── Pipeline ingest ───────────────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    stock_codes: list[str]
    question: str = ""
    max_articles: int = 4
    threshold: float = 0.6
    try_idx_pdf: bool = True


@app.post("/api/pipeline")
async def run_pipeline(req: PipelineRequest):
    """
    Jalankan full ingest pipeline:
      fetch financial statement 3 tahun → crawl news → extract → filter → build per-year graph.

    Operasi blocking (sync libraries: yfinance, feedparser); jalankan via asyncio.to_thread.
    """
    import asyncio

    def _work():
        keywords: dict[str, list[str]] = {}
        articles: dict = {}
        seed = req.question or f"Analisis kinerja {' '.join(req.stock_codes)} di BEI"

        financial = fetch_multiple(req.stock_codes, try_idx_pdf=req.try_idx_pdf)

        for code in req.stock_codes:
            kws = extract_search_keywords(code, seed, n=3)
            keywords[code] = kws
            articles[code] = crawl_by_keywords(kws, code, max_total=req.max_articles)

        extractions = extract_all(articles, financial)
        company_names = {c: d.company_name for c, d in financial.items()}
        checked = filter_results(extractions, company_names=company_names, threshold=req.threshold)
        stats = build_graph_multi_tenant(checked, articles, financial)

        return {
            "keywords": keywords,
            "articles_count": {k: len(v) for k, v in articles.items()},
            "financial_count": len(financial),
            "graphs_built": [
                {
                    "year": year,
                    "graph_name": s.graph_name,
                    "nodes_created": s.nodes_created,
                    "edges_created": s.edges_created,
                    "errors": s.errors,
                }
                for year, s in stats.items()
            ],
        }

    result = await asyncio.to_thread(_work)

    # Refresh available_years setelah ingest selesai
    if _engine is not None:
        await _engine.initialize()

    return result


# ── WebSocket Chat ────────────────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """
    Bidirectional chat. Client kirim JSON {session_id, question}.
    Server stream balik sequence of events (plan, agent_start, agent_done, token, final).
    """
    await websocket.accept()

    if _orchestrator is None:
        await websocket.send_json({"type": "error", "message": "orchestrator not initialized"})
        await websocket.close()
        return

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "invalid json"})
                continue

            session_id = payload.get("session_id") or "default"
            question = (payload.get("question") or "").strip()
            if not question:
                await websocket.send_json({"type": "error", "message": "empty question"})
                continue

            # Reset history
            if payload.get("reset"):
                _session_store.clear(session_id)
                await websocket.send_json({"type": "history_cleared"})
                continue

            try:
                async for event in _orchestrator.run_stream(session_id, question):
                    await websocket.send_json(event)
            except Exception as exc:
                await websocket.send_json({
                    "type": "error", "message": f"{type(exc).__name__}: {exc}",
                })

    except WebSocketDisconnect:
        return


@app.get("/api/history/{session_id}")
async def get_history(session_id: str):
    history = _session_store.history(session_id)
    return {
        "session_id": session_id,
        "turns": [
            {
                "role": msg.get("role", "assistant"),
                "content": msg.get("content", ""),
            }
            for msg in history
        ],
    }


@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str):
    _session_store.clear(session_id)
    return {"cleared": session_id}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app.services.ui.server:app", host="0.0.0.0", port=port, reload=True)
