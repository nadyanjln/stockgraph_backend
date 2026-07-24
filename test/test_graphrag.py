"""
Smoke test untuk GraphRAG-SDK engine + 3-agent orchestrator.

Prasyarat:
  1. Docker FalkorDB jalan: docker run -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
  2. Graph sudah diisi via test_graph.py atau via API /api/pipeline.
  3. OPENAI_API_KEY ter-set di .env

Run:
  uv run python tests/test_graphrag.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from agents.orchestrator import Orchestrator, SessionStore
from graph.graphrag_engine import GraphRAGEngine


QUESTIONS = [
    "Siapa yang memimpin Bank Central Asia (BBCA)?",
    "Berapa laba bersih BBRI tahun terakhir?",
    "Bagaimana kebijakan BI Rate mempengaruhi saham perbankan?",
    "Bandingkan kinerja BBCA dan BBRI",
]


async def main():
    async with GraphRAGEngine() as engine:
        if not engine.available_years:
            print("Tidak ada year graph. Jalankan pipeline ingest dulu.")
            return

        print(f"Years tersedia: {engine.available_years}")
        orch = Orchestrator(engine, session_store=SessionStore())
        session_id = "smoke_test"

        for q in QUESTIONS:
            print(f"\n{'=' * 70}\nQ: {q}\n")
            tokens = []
            async for ev in orch.run_stream(session_id, q):
                if ev["type"] == "plan":
                    print(f"[plan] agents={ev['agents']} year={ev['year']}")
                elif ev["type"] == "agent_start":
                    print(f"[start] {ev['agent']}")
                elif ev["type"] == "agent_done":
                    print(f"[done]  {ev['agent']}: {ev['preview'][:80]}")
                elif ev["type"] == "token":
                    tokens.append(ev["delta"])
                elif ev["type"] == "final":
                    print(f"\nFinal answer:\n{ev['answer']}")
                    if ev.get("citations"):
                        print(f"Citations: {ev['citations']}")
                elif ev["type"] == "error":
                    print(f"[error] {ev['message']}")


if __name__ == "__main__":
    asyncio.run(main())
