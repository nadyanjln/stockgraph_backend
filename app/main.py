"""FastAPI entry point — unified chatbot + StockGraph GraphRAG backend.

This single app serves both:
  * the CRUD/auth API (PostgreSQL): users, conversations, messages — prefix /api/v1
  * the StockGraph GraphRAG engine: /api/years, /api/query, /api/key-financials,
    /api/merger/pipeline, the /ws/chat WebSocket, and /api/history/{session_id}

The GraphRAG engine is initialised best-effort: the app still boots (and the
CRUD/auth API works) even when FalkorDB or the LLM provider are unavailable.
Graph *queries* only need FalkorDB at request time and fail gracefully.

Run:
    uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.agent.response_formatter import format_rag_response
from app.database.session import dispose_db, init_db
from app.database.session import async_session_factory
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes import conversations, messages, users
from app.schemas.common import ErrorResponse
from app.utils.exceptions import AppError
from app.utils.logger import get_logger
from app.services.supabase_auth_service import SupabaseAuthService
from app.services.user_service import UserService

logger = get_logger("chatbot.app")


async def _setup_engine(app: FastAPI) -> None:
    """Best-effort GraphRAG engine + orchestrator init. Never fatal."""
    app.state.engine = None
    app.state.orchestrator = None
    app.state.session_store = None
    try:
        from app.core.agent.orchestrator import Orchestrator, SessionStore
        from app.services.database.graphrag_engine import GraphRAGEngine

        engine = GraphRAGEngine()
        await engine.initialize()
        store = SessionStore()
        app.state.engine = engine
        app.state.orchestrator = Orchestrator(engine, session_store=store)
        app.state.session_store = store
        if engine.is_available:
            logger.info("GraphRAG engine ready — years=%s", engine.available_years)
        else:
            logger.warning(
                "GraphRAG engine started in degraded mode — FalkorDB is offline"
            )
    except Exception as exc:  # noqa: BLE001 — engine is optional
        logger.warning(
            "GraphRAG engine unavailable; chat falls back to a stub reply (%s: %s)",
            type(exc).__name__,
            exc,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting up — running create_all")
    await init_db()
    await _setup_engine(app)
    yield
    logger.info("shutting down — disposing engine")
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        try:
            await engine.close()
        except Exception:  # noqa: BLE001
            pass
    await dispose_db()


def _mount_engine_routes(app: FastAPI) -> None:
    """Include the StockGraph engine routers. Guarded so a missing optional
    dependency cannot prevent the CRUD/auth API from starting."""
    try:
        from app.routes.endpoint import router as endpoint_router
        from app.routes.merger_routes import router as merger_router

        app.include_router(endpoint_router)
        app.include_router(merger_router)
        logger.info("StockGraph engine routes mounted")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "engine routes not mounted (%s: %s)", type(exc).__name__, exc
        )


async def _fallback_reply(question: str) -> str:
    from app.utils.bot import generate_reply

    base = await generate_reply(question)
    return (
        f"{base}\n\n(Catatan: mesin GraphRAG belum aktif — pastikan FalkorDB "
        "berjalan dan graph sudah dibangun lewat pipeline.)"
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="StockGraph + Chatbot Backend API",
        version="1.1.0",
        description="Async FastAPI backend: PostgreSQL CRUD/auth + GraphRAG engine.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_prefix = "/api/v1"
    app.include_router(users.router, prefix=api_prefix)
    app.include_router(conversations.router, prefix=api_prefix)
    app.include_router(messages.router, prefix=api_prefix)

    _mount_engine_routes(app)

    @app.get("/health", tags=["health"])
    async def health(request: Request) -> dict:
        engine = getattr(request.app.state, "engine", None)
        return {
            "status": "ok",
            "engine_ready": bool(engine and engine.is_available),
            "engine_configured": engine is not None,
            "engine_error": engine.connection_error if engine else "",
            "years": engine.available_years if engine else [],
        }

    # ── Pipeline alias ───────────────────────────────────────────────────────
    @app.post("/api/pipeline", tags=["endpoint"])
    async def run_pipeline_alias(
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> JSONResponse:
        """Alias for /api/merger/pipeline kept for frontend compatibility."""
        from app.routes.merger_routes import (
            MergerPipelineRequest,
            run_merger_pipeline,
        )

        body = await request.json()
        result = await run_merger_pipeline(
            MergerPipelineRequest(**body),
            request,
            current_user,
        )
        return JSONResponse(content=result)

    # ── WebSocket chat ───────────────────────────────────────────────────────
    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket) -> None:
        token = websocket.query_params.get("access_token", "")
        try:
            identity = await asyncio.to_thread(
                SupabaseAuthService().authenticate,
                token,
            )
            async with async_session_factory() as auth_session:
                user = await UserService(auth_session).sync_supabase_user(identity)
        except AppError:
            await websocket.close(code=4401, reason="invalid session")
            return
        await websocket.accept()
        orchestrator = getattr(websocket.app.state, "orchestrator", None)
        session_store = getattr(websocket.app.state, "session_store", None)

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json(
                        {"type": "error", "message": "invalid json"}
                    )
                    continue

                client_session_id = payload.get("session_id") or "default"
                session_id = f"user:{user.id}:{client_session_id}"
                question = (payload.get("question") or "").strip()

                if payload.get("reset"):
                    if session_store is not None:
                        session_store.clear(session_id)
                    await websocket.send_json({"type": "history_cleared"})
                    continue

                if not question:
                    await websocket.send_json(
                        {"type": "error", "message": "empty question"}
                    )
                    continue

                if orchestrator is None:
                    # Engine offline → stream a single fallback "final" event so
                    # the UI still shows an answer instead of hanging.
                    reply = await _fallback_reply(question)
                    formatted = format_rag_response(reply, [])
                    await websocket.send_json(
                        {"type": "plan", "agents": [], "year": None}
                    )
                    await websocket.send_json(
                        {"type": "final", "answer": reply, "citations": [], **formatted}
                    )
                    continue

                # Always end the turn with a "final" event so the client can
                # render and persist an answer, even when the engine errors
                # (e.g. FalkorDB offline). Engine errors become a fallback reply.
                got_final = False
                try:
                    async for event in orchestrator.run_stream(session_id, question):
                        if event.get("type") == "error":
                            reply = await _fallback_reply(question)
                            formatted = format_rag_response(reply, [])
                            await websocket.send_json(
                                {"type": "final", "answer": reply, "citations": [], **formatted}
                            )
                            got_final = True
                            break
                        if event.get("type") == "final":
                            got_final = True
                        await websocket.send_json(event)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("run_stream failed: %s: %s", type(exc).__name__, exc)

                if not got_final:
                    reply = await _fallback_reply(question)
                    formatted = format_rag_response(reply, [])
                    await websocket.send_json(
                        {"type": "final", "answer": reply, "citations": [], **formatted}
                    )
        except WebSocketDisconnect:
            return

    # ── Session history (in-memory, engine sessions) ─────────────────────────
    @app.get("/api/history/{session_id}", tags=["endpoint"])
    async def get_history(
        session_id: str,
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> dict:
        store = getattr(request.app.state, "session_store", None)
        session_key = f"user:{current_user.id}:{session_id}"
        if store is None:
            return {"session_id": session_id, "turns": []}
        return {
            "session_id": session_id,
            "turns": [
                {"role": m.get("role", "assistant"), "content": m.get("content", "")}
                for m in store.history(session_key)
            ],
        }

    @app.delete("/api/history/{session_id}", tags=["endpoint"])
    async def clear_history(
        session_id: str,
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> dict:
        store = getattr(request.app.state, "session_store", None)
        if store is not None:
            store.clear(f"user:{current_user.id}:{session_id}")
        return {"cleared": session_id}

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                message=exc.message,
                code=exc.code,
                details=exc.details,
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Keep only JSON-safe fields. Pydantic v2 puts the original exception
        # object in each error's `ctx`, which is not serializable.
        errors = [
            {
                "loc": [str(part) for part in err.get("loc", [])],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        # Surface the first field message so the UI shows something specific.
        first_msg = errors[0]["msg"] if errors else "validation failed"
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                message=first_msg,
                code="validation_error",
                details={"errors": errors},
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                message="internal server error",
                code="internal_error",
            ).model_dump(),
        )

    return app


app = create_app()
