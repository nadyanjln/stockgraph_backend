import os
import sys

backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_root not in sys.path:
    sys.path.insert(0, backend_root)

try:
    from app.main import create_app
    app = create_app()
except Exception as exc:
    import traceback
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="StockGraph Diagnostic App")
    err_str = traceback.format_exc()

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def catch_all(full_path: str):
        return JSONResponse(
            status_code=500,
            content={
                "error": "DIAGNOSTIC_CATCH_ALL",
                "exception": str(exc),
                "traceback": err_str
            }
        )
