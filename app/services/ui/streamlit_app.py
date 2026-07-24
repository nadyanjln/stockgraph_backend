"""Streamlit UI for StockGraph with separate Beranda and Hasil pages."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import streamlit as st
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.agent.orchestrator import Orchestrator, SessionStore
from app.core.extractor.llm_extractor import extract_all, extract_search_keywords
from app.core.extractor.relevance_checker import filter_results
from app.services.crawler.financial_fetcher import fetch_multiple
from app.services.crawler.news_crawler import crawl_by_keywords
from app.services.database.graph_builder import build_graph_multi_tenant
from app.services.database.graphrag_engine import GraphRAGEngine

load_dotenv()


def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


@st.cache_resource
def get_engine() -> GraphRAGEngine:
    engine = GraphRAGEngine()
    run_async(engine.initialize())
    return engine


@st.cache_resource
def get_session_store() -> SessionStore:
    return SessionStore()


def get_orchestrator() -> Orchestrator:
    return Orchestrator(get_engine(), session_store=get_session_store())


def run_pipeline(stock_codes: list[str], question: str, max_articles: int, threshold: float, try_idx_pdf: bool) -> dict:
    keywords: dict[str, list[str]] = {}
    articles: dict = {}
    seed = question or f"Analisis kinerja {' '.join(stock_codes)} di BEI"

    financial = fetch_multiple(stock_codes, try_idx_pdf=try_idx_pdf)

    for code in stock_codes:
        kws = extract_search_keywords(code, seed, n=3)
        keywords[code] = kws
        articles[code] = crawl_by_keywords(kws, code, max_total=max_articles)

    extractions = extract_all(articles, financial)
    company_names = {code: data.company_name for code, data in financial.items()}
    checked = filter_results(extractions, company_names=company_names, threshold=threshold)
    stats = build_graph_multi_tenant(checked, articles, financial)
    run_async(get_engine().initialize())

    return {
        "keywords": keywords,
        "articles_count": {code: len(items) for code, items in articles.items()},
        "financial_count": len(financial),
        "graphs_built": [
            {
                "year": year,
                "graph_name": stat.graph_name,
                "documents_ingested": stat.documents_ingested,
                "nodes_created": stat.nodes_created,
                "edges_created": stat.edges_created,
                "errors": stat.errors,
            }
            for year, stat in stats.items()
        ],
    }


async def stream_answer(session_id: str, question: str):
    async for event in get_orchestrator().run_stream(session_id, question):
        yield event


def init_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = f"st-{uuid4().hex[:10]}"
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "prompt_text" not in st.session_state:
        st.session_state.prompt_text = ""
    if "thinking_log" not in st.session_state:
        st.session_state.thinking_log = []
    if "page" not in st.session_state:
        st.session_state.page = "beranda"
    if "request_payload" not in st.session_state:
        st.session_state.request_payload = None
    if "result_ready" not in st.session_state:
        st.session_state.result_ready = False
    if "result_answer" not in st.session_state:
        st.session_state.result_answer = ""
    if "result_citations" not in st.session_state:
        st.session_state.result_citations = []
    if "last_pipeline" not in st.session_state:
        st.session_state.last_pipeline = None


def common_style() -> None:
    st.markdown(
        """
        <style>
            .stApp {
                background: radial-gradient(circle at 20% 0%, #1b1f2e 0%, #101217 45%, #0b0d12 100%);
                color: #f2f3f5;
            }
            .block-container {
                max-width: 1120px;
                padding-top: 1.8rem;
                padding-bottom: 2rem;
            }
            h1, h2, h3, p, label, div, span { color: #f2f3f5; }
            .sg-card {
                border: 1px solid #2a2e3a;
                border-radius: 18px;
                background: linear-gradient(145deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02));
                padding: 1rem 1.1rem;
                margin-bottom: 0.8rem;
            }
            .sg-title {
                font-size: 2.6rem;
                text-align: center;
                margin-bottom: 1rem;
                font-weight: 500;
            }
            .sg-title b { font-weight: 700; }
            .stButton button {
                border-radius: 14px !important;
                border: 1px solid #30374a !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_beranda() -> None:
    st.markdown("<div class='sg-title'>Mulai dari <b>pertanyaan</b>, temukan <b>insight</b>-nya.</div>", unsafe_allow_html=True)

    stock_choices = [
        "BBCA", "BBRI", "BMRI", "BBNI", "TLKM", "ASII",
        "UNVR", "ICBP", "CPIN", "ADRO", "MDKA", "ANTM",
    ]
    selected_code = st.selectbox("Tambahkan kode saham....", stock_choices, index=0)

    col_a, col_b = st.columns(2)
    with col_a:
        max_articles = st.slider("Maksimal artikel", 1, 10, 5)
    with col_b:
        threshold = st.slider("Relevance threshold", 0.0, 1.0, 0.5, 0.05)

    st.markdown("<div class='sg-card'><b>Rekomendasi pertanyaan</b></div>", unsafe_allow_html=True)
    suggested_prompts = [
        "Bagaimana prospek saham ini dalam beberapa bulan ke depan?",
        "Apa risiko utama yang sedang memengaruhi kinerja saham ini?",
        "Apakah sentimen berita terbaru terhadap saham ini cenderung positif atau negatif?",
    ]
    sp1, sp2, sp3 = st.columns(3)
    if sp1.button(suggested_prompts[0], use_container_width=True):
        st.session_state.prompt_text = suggested_prompts[0]
        st.rerun()
    if sp2.button(suggested_prompts[1], use_container_width=True):
        st.session_state.prompt_text = suggested_prompts[1]
        st.rerun()
    if sp3.button(suggested_prompts[2], use_container_width=True):
        st.session_state.prompt_text = suggested_prompts[2]
        st.rerun()

    prompt = st.text_area("Pertanyaan", value=st.session_state.prompt_text, placeholder="Tanyakan apapun...", height=160)

    c1, c2 = st.columns([1, 5])
    with c1:
        submit = st.button("Kirim", type="primary", use_container_width=True)
    with c2:
        reset_chat = st.button("Reset chat", use_container_width=True)

    if reset_chat:
        get_session_store().clear(st.session_state.session_id)
        st.session_state.messages = []
        st.session_state.result_ready = False
        st.session_state.result_answer = ""
        st.session_state.result_citations = []
        st.rerun()

    if submit:
        if not prompt.strip():
            st.error("Pertanyaan tidak boleh kosong.")
            return

        st.session_state.prompt_text = prompt
        st.session_state.request_payload = {
            "selected_code": selected_code,
            "question": prompt.strip(),
            "max_articles": max_articles,
            "threshold": threshold,
        }
        st.session_state.result_ready = False
        st.session_state.result_answer = ""
        st.session_state.result_citations = []
        st.session_state.thinking_log = [
            "Menyiapkan konteks pertanyaan dan kode saham.",
            "Memulai pengambilan data finansial dan berita.",
            "Menganalisis hubungan antarfakta pada graph.",
        ]
        st.session_state.page = "hasil"
        st.rerun()


def render_hasil() -> None:
    payload = st.session_state.request_payload
    if not payload:
        st.warning("Belum ada request. Kembali ke beranda untuk memulai.")
        if st.button("Kembali ke Beranda"):
            st.session_state.page = "beranda"
            st.rerun()
        return

    top_left, top_right = st.columns([4, 1])
    with top_left:
        st.markdown(f"### Hasil - {payload['selected_code']}")
    with top_right:
        if st.button("Beranda", use_container_width=True):
            st.session_state.page = "beranda"
            st.rerun()

    left_col, right_col = st.columns([2.2, 1.2])
    with left_col:
        st.markdown("<div class='sg-card'><b>Hasil Analisis</b></div>", unsafe_allow_html=True)
        st.caption(f"Pertanyaan: {payload['question']}")
        answer_box = st.empty()
        process_box = st.empty()
        answer_box.info("Buffering jawaban... model sedang menganalisis data.")
        process_box.markdown("### Ringkasan proses berpikir\n- " + "\n- ".join(st.session_state.thinking_log))

    with right_col:
        st.markdown("<div class='sg-card'><b>Insight Cepat</b><br/>Sedang dihitung...</div>", unsafe_allow_html=True)
        st.markdown("<div class='sg-card'><b>Knowledge Graph</b><br/>Membangun relasi entitas...</div>", unsafe_allow_html=True)
        key_fin_box = st.empty()
        key_fin_box.markdown("### Key Financials\nBuffering data finansial...")

    if not st.session_state.result_ready:
        with st.status("Menyiapkan data dan menjalankan analisis...", expanded=True) as status:
            st.write("1. Mengambil financial statement terbaru")
            st.write("2. Crawling dan filtering berita relevan")
            st.write("3. Menjalankan reasoning multi-agent")
            try:
                st.session_state.last_pipeline = run_pipeline(
                    [payload["selected_code"]],
                    payload["question"],
                    payload["max_articles"],
                    payload["threshold"],
                    try_idx_pdf=True,
                )
                status.update(label="Pipeline selesai", state="complete", expanded=False)
            except Exception as exc:
                status.update(label="Pipeline gagal", state="error", expanded=True)
                st.error(f"Gagal menjalankan pipeline: {type(exc).__name__}: {exc}")

        pipeline_result = st.session_state.last_pipeline or {}
        article_count = (pipeline_result.get("articles_count") or {}).get(payload["selected_code"], 0)
        key_fin_box.markdown(
            "### Key Financials\n"
            f"- Artikel dianalisis: {article_count}\n"
            f"- Kode saham: {payload['selected_code']}\n"
            f"- Relevance threshold: {payload['threshold']:.2f}"
        )

        full_prompt = f"[{payload['selected_code']}] {payload['question']}"
        st.session_state.messages.append({"role": "user", "content": full_prompt})

        citations: list[str] = []
        answer_parts: list[str] = []

        async def consume():
            async for event in stream_answer(st.session_state.session_id, full_prompt):
                event_type = event.get("type")
                if event_type == "plan":
                    st.session_state.thinking_log.append(
                        f"Merencanakan agent: {', '.join(event.get('agents', []))} (year={event.get('year')})."
                    )
                    process_box.markdown("### Ringkasan proses berpikir\n- " + "\n- ".join(st.session_state.thinking_log))
                elif event_type == "agent_done":
                    st.session_state.thinking_log.append(f"{event.get('agent')} selesai: {event.get('preview', '')}")
                    process_box.markdown("### Ringkasan proses berpikir\n- " + "\n- ".join(st.session_state.thinking_log))
                elif event_type == "token":
                    answer_parts.append(event.get("delta", ""))
                    answer_box.markdown("".join(answer_parts) + "|")
                elif event_type == "final":
                    citations.extend(event.get("citations", []))
                    answer_box.markdown(event.get("answer") or "".join(answer_parts))
                elif event_type == "error":
                    answer_box.error(event.get("message", "Terjadi error"))

        run_async(consume())

        final_answer = "".join(answer_parts).strip() or "Tidak ada jawaban yang berhasil dibuat."
        st.session_state.result_answer = final_answer
        st.session_state.result_citations = citations
        st.session_state.result_ready = True
        st.session_state.messages.append({
            "role": "assistant",
            "content": final_answer,
            "citations": citations,
        })
    else:
        answer_box.markdown(st.session_state.result_answer)
        if st.session_state.result_citations:
            with st.expander("Sumber"):
                for citation in st.session_state.result_citations:
                    st.write(citation)


st.set_page_config(page_title="StockGraph", page_icon=":bar_chart:", layout="wide")
init_state()
common_style()

if st.session_state.page == "beranda":
    render_beranda()
else:
    render_hasil()
