"""Stub bot reply generator.

Replace `generate_reply` with a real LLM/GraphRAG call when integrating
with the StockGraph engine.
"""

from __future__ import annotations


async def generate_reply(user_message: str) -> str:
    """Return a canned reply. Plug your LLM/RAG pipeline here."""
    text = user_message.strip()
    if not text:
        return "Maaf, pesan kosong. Coba ketik sesuatu."
    return f"Echo bot: kamu bilang '{text}'. (Ganti generator ini dengan LLM kamu.)"
