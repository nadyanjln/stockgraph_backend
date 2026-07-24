from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="StockGraph Minimal API")

@app.get("/health")
async def health():
    return {"status": "ok", "message": "Vercel Python Serverless is ALIVE!"}

@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def catch_all(full_path: str):
    return {"status": "ok", "path": full_path}
