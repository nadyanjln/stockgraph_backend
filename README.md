# StockGraph Backend API

FastAPI backend service untuk StockGraph: menyajikan PostgreSQL CRUD/Auth API dan GraphRAG engine (FalkorDB + OpenAI).

## Requirements

- Python 3.11+
- PostgreSQL
- FalkorDB (Redis module)
- `uv` (Python package manager, disarankan) atau `pip`

## Quick Start (Lokal)

1. **Salin Environment Variables**
   ```bash
   cp .env.example .env
   # Edit .env sesuai konfigurasi PostgreSQL, FalkorDB, dan OpenAI API Key Anda
   ```

2. **Inisialisasi Database PostgreSQL**
   ```bash
   psql -U postgres -d chatbot_db -f database.sql
   ```

3. **Install Dependensi & Jalankan Backend**
   Dengan `uv`:
   ```bash
   uv sync
   uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```
   Atau dengan standard `pip`:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # macOS/Linux
   pip install -r requirements.txt
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

4. **Uji Endpoint Health**
   Buka browser di `http://localhost:8000/health` atau `http://localhost:8000/docs` (Swagger UI).

## Panduan Deployment Cloud (Render / Railway / Fly.io)

### Render.com
1. Buat **Web Service** baru di Render dari repositori ini.
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Set Environment Variables yang dibutuhkan dari `.env.example` (DB_HOST, OPENAI_API_KEY, dll.).

### Railway / Docker
1. Hubungkan repositori ke Railway.
2. Tambahkan layanan PostgreSQL & Redis/FalkorDB.
3. Atur environment variables sesuai service internal URL Railway.
