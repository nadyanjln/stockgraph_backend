"""Manager agent for routing and final synthesis."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.agent.common import (
    AgentName,
    ChatMessage,
    MANAGER_MODEL,
    chat_complete,
    stream_chat,
)
from app.core.agent.evidence_policy import coverage_instruction

MANAGER_ROUTER_SYSTEM = """\
Kamu adalah Manager Agent yang merencanakan strategi menjawab pertanyaan analisis saham BEI.
Pertanyaan akan dijawab oleh dua spesialis: News Analyst dan Financial Analyst.

Tugasmu: putuskan agent mana yang perlu dipanggil. Output JSON ketat:
{"agents": ["news"] | ["financial"] | ["news", "financial"], "year": <int>, "rationale": "..."}

Aturan:
- "news"      jika pertanyaan tentang peristiwa, kebijakan, sentimen, tokoh.
- "financial" jika pertanyaan tentang angka, laba, ROE, EPS, perbandingan kinerja.
- keduanya    untuk pertanyaan komprehensif (analisis menyeluruh, prospek, valuasi).
- year        tahun fiskal target (default tahun terbaru yang tersedia).
"""


MANAGER_SYNTHESIZER_SYSTEM = """\
Kamu adalah analis saham BEI yang menyusun jawaban final berdasarkan evidence
retrieval yang diberikan. Tulislah seperti analis profesional yang berbicara
secara natural kepada investor pemula atau menengah, bukan seperti mengisi template.

Aturan:
- Jawab HANYA menggunakan context yang diberikan.
- Jangan gunakan pengetahuan luar, asumsi, atau informasi yang tidak muncul pada context.
- Pertanyaan investor saat ini adalah sumber kebenaran utama. Abaikan riwayat
  percakapan bila ticker, emiten, tahun, atau topiknya berbeda dari pertanyaan saat ini.
- Setiap klaim faktual harus didukung oleh context retrieval yang tersedia.
- Jika context tidak memuat informasi yang cukup, nyatakan secara eksplisit bahwa
  dokumen yang tersedia tidak memuat informasi yang cukup.
- Prioritaskan laporan keuangan resmi dan data keuangan terstruktur dibanding berita
  bila keduanya tersedia.
- Jangan menciptakan angka, entitas, tanggal, hubungan, penyebab, atau rekomendasi.
- Kutip nilai keuangan persis seperti tertulis pada context.
- Pertahankan makna, fakta, angka, dan kesimpulan evidence secara persis.
  Jangan menambah, menghapus, atau mengubah informasi; perbaiki hanya penyajian
  dan keterbacaannya.
- Abaikan informasi yang berulang atau duplikat.
- Integrasikan evidence laporan keuangan, berita, dan knowledge graph ke dalam
  penjelasan yang utuh; jangan menempelkan fakta sebagai daftar yang terpisah-pisah.
- Jawab inti pertanyaan secara langsung pada kalimat atau paragraf pertama.
- Fokus hanya pada pertanyaan pengguna dan pilih tingkat detail yang sesuai.
- Jelaskan angka keuangan dengan bahasa sederhana tanpa mengubah nilainya.
- Jangan mengulang angka atau fakta yang sudah dijelaskan kecuali diperlukan
  untuk membandingkan atau menarik implikasi yang didukung evidence.
- Jika pengguna meminta analisis, susun narasi yang cukup komprehensif dan jelaskan
  hubungan antar-evidence, peluang, serta risiko yang benar-benar tersedia.
- Jika pengguna meminta perbandingan, gunakan tabel Markdown hanya jika evidence
  menyediakan atribut yang sebanding. Jelaskan temuan penting setelah tabel.
- Jika pengguna meminta daftar atau ringkasan beberapa temuan, gunakan bullet point
  singkat. Jika pengguna meminta langkah-langkah, gunakan numbered list.
- Untuk pertanyaan definisi atau fakta sederhana, jawab langsung dalam satu atau
  dua paragraf pendek tanpa heading yang tidak perlu.
- Untuk pertanyaan kelayakan membeli atau prospek investasi, berikan jawaban awal
  yang tegas tetapi bersyarat, lalu jelaskan dasar fundamental, berita, peluang,
  risiko, dan keterbatasan evidence. Jangan memberi kepastian atau rekomendasi
  investasi yang tidak didukung context.
- Kembalikan hanya Markdown yang sudah diformat, tanpa komentar tentang proses
  pemformatan dan tanpa membungkus seluruh jawaban dalam code block.
- Gunakan Markdown yang bersih dan konsisten. Pecah narasi menjadi paragraf pendek,
  maksimal 2-3 kalimat per paragraf, dengan baris kosong antarparagraf.
- Gunakan heading `##` untuk bagian utama hanya ketika jawaban memang memiliki
  beberapa bagian logis. Pilih judul deskriptif seperti Ringkasan, Analisis,
  Data Keuangan, Faktor Pendukung, Risiko, atau Kesimpulan hanya jika relevan;
  hilangkan bagian yang tidak berlaku.
- Hindari paragraf panjang yang menumpuk banyak angka, peluang, dan risiko sekaligus.
  Pindahkan enumerasi, kelompok fakta, kelebihan, kekurangan, rekomendasi, atau
  argumen pendukung ke bullet point bila sesuai.
- Gunakan numbered list hanya untuk langkah atau prosedur yang benar-benar berurutan.
- Sajikan metrik keuangan, nilai numerik, perbandingan, atau data terstruktur dalam
  tabel Markdown bila bentuk tabel membuat evidence lebih jelas dan atributnya
  memang sebanding.
- Setiap bullet harus menyampaikan satu gagasan utama dan tidak mengulang paragraf.
- Gunakan **bold** hanya untuk ticker atau nama perusahaan, angka atau metrik penting,
  dan simpulan utama; jangan menebalkan seluruh kalimat atau setiap bullet.
- Jangan menggunakan HTML.
- Heading deskriptif boleh digunakan untuk jawaban panjang yang memiliki beberapa
  topik berbeda, tetapi jangan membuat heading untuk jawaban sederhana.
- Untuk analisis saham yang kompleks, bentuk yang disarankan adalah pembuka singkat,
  kelompok fakta utama yang mudah dipindai, lalu penilaian akhir yang natural.
  Ini adalah panduan keterbacaan, bukan template wajib.
- Jangan pernah menggunakan heading "Monolog", "Poin-poin", "Kesimpulan",
  atau "Penutup".
- Tutup secara natural dengan penilaian ringkas atau hal penting yang perlu
  dipantau, tanpa heading penutup khusus.
- Hindari penjelasan bertele-tele, tetapi jangan terlalu pendek bila pertanyaan
  membutuhkan interpretasi.
- Jangan berspekulasi.
- Jangan menulis daftar sumber, label sumber, URL, atau nomor sitasi seperti [1]
  di dalam narasi. UI menampilkan referensi yang digunakan secara terpisah di akhir.
"""


@dataclass
class ManagerPlan:
    agents: list[AgentName]
    year: int
    rationale: str


async def manager_plan(
    question: str,
    history: list[ChatMessage],
    available_years: list[int],
) -> ManagerPlan:
    """Decide which specialist agents should answer and which year to target."""
    default_year = available_years[-1] if available_years else 0

    years_str = ", ".join(str(y) for y in available_years) or "(belum ada)"
    messages: list[ChatMessage] = [
        {"role": "system", "content": MANAGER_ROUTER_SYSTEM},
        *history[-6:],
        {"role": "user", "content": (
            f"Pertanyaan: {question}\n"
            f"Tahun yang tersedia di graph: {years_str}\n"
            f"Default year: {default_year}\n\n"
            f"Output JSON saja."
        )},
    ]

    try:
        raw = await chat_complete(
            messages,
            model=MANAGER_MODEL,
            temperature=0.0,
            response_format={"type": "json_object"},
            purpose="Query Analysis and Agent Routing",
            caller="app.core.agent.manager_agent.manager_plan",
        )
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        plan = json.loads(raw)
        agents = [a for a in plan.get("agents", []) if a in ("news", "financial")]
        comprehensive_query = any(
            keyword in question.lower()
            for keyword in (
                "prospek",
                "risiko",
                "faktor",
                "analisis mendalam",
                "fundamental dan berita",
                "kondisi pasar",
            )
        )
        if comprehensive_query:
            agents = ["news", "financial"]
        if not agents:
            agents = ["news", "financial"]
        year = int(plan.get("year") or default_year)
        if available_years and year not in available_years:
            year = default_year
        return ManagerPlan(
            agents=agents,
            year=year,
            rationale=str(plan.get("rationale", ""))[:200],
        )
    except Exception as exc:
        print(f"[manager_plan] fallback ({exc})")
        return ManagerPlan(
            agents=["news", "financial"],
            year=default_year,
            rationale="default: panggil keduanya",
        )


def manager_synthesizer_messages(
    question: str,
    history: list[ChatMessage],
    sub_answers: dict[str, str],
    sub_citations: list[str],
    sources: list[dict[str, str]] | None = None,
) -> list[ChatMessage]:
    """Build messages for the final synthesizer call."""
    parts = []
    if "news" in sub_answers:
        parts.append(f"## Jawaban News Analyst:\n{sub_answers['news']}")
    if "financial" in sub_answers:
        parts.append(f"## Jawaban Financial Analyst:\n{sub_answers['financial']}")
    source_items = sources or []
    source_lines = []
    for index, source in enumerate(source_items[:8], start=1):
        title = source.get("title") or source.get("source_name") or source.get("url") or f"Sumber {index}"
        publisher = source.get("source_name") or ""
        date = source.get("publication_date") or ""
        source_type = source.get("source_type") or ""
        reporting_period = source.get("reporting_period") or ""
        label = (
            "[Laporan Keuangan IDX]"
            if source_type == "financial_report"
            else "[Berita]"
            if source_type == "news"
            else "[Sumber]"
        )
        snippet = source.get("snippet") or source.get("retrieved_text") or ""
        source_lines.append(
            f"[{index}] {label} {title}\n"
            f"Publisher: {publisher or '-'}\n"
            f"Tanggal/Periode: {date or reporting_period or '-'}\n"
            f"URL: {source.get('url') or '-'}\n"
            f"Cuplikan: {snippet[:700] or '-'}"
        )
    citations_block = "\n\n".join(source_lines) or "(tidak ada)"
    coverage_rule = coverage_instruction(source_items)

    return [
        {"role": "system", "content": MANAGER_SYNTHESIZER_SYSTEM},
        *history[-6:],
        {"role": "user", "content": (
            f"Pertanyaan investor: {question}\n\n"
            f"{chr(10).join(parts)}\n\n"
            f"Sumber retrieval bernomor:\n{citations_block}\n\n"
            f"Kebijakan cakupan evidence:\n{coverage_rule}\n\n"
            "Susun jawaban final yang profesional, natural, dan mudah dipahami oleh awam. "
            "Pilih bentuk narasi, bullet, atau tabel berdasarkan intent pertanyaan; "
            "jangan gunakan struktur template yang sama untuk semua jawaban. "
            "Pertahankan makna, fakta, angka, dan kesimpulan evidence tanpa menambah, "
            "menghapus, atau mengubah informasi. Gunakan Markdown yang mudah dipindai "
            "dan batasi setiap paragraf maksimal 2-3 kalimat. Gunakan heading ## hanya "
            "untuk bagian utama yang relevan, bullet untuk kelompok fakta atau argumen, "
            "numbered list hanya untuk langkah berurutan, serta tabel Markdown untuk "
            "metrik, angka, atau perbandingan yang sesuai. Gunakan **bold** hanya untuk "
            "ticker atau perusahaan, angka atau metrik penting, dan simpulan utama. "
            "Jangan gunakan HTML atau membungkus seluruh jawaban dalam code block. "
            "Gunakan hanya informasi dari jawaban spesialis dan sumber retrieval bernomor di atas. "
            "Abaikan riwayat percakapan jika berbeda ticker, emiten, atau topik. "
            "Jangan memasukkan klaim yang tidak muncul eksplisit pada evidence. "
            "Jangan menulis heading Monolog, Poin-poin, Kesimpulan, atau Penutup. "
            "Jangan menulis daftar sumber maupun nomor sitasi di dalam jawaban karena "
            "referensi sudah ditampilkan secara terpisah oleh UI. "
            "Jika informasi pendukung tidak tersedia, tulis bahwa dokumen yang "
            "tersedia tidak memuat informasi yang cukup. Kembalikan hanya Markdown "
            "yang telah diformat."
        )},
    ]


async def synthesize_answer(messages: list[ChatMessage]) -> str:
    """Non-streaming final manager synthesis."""
    return await chat_complete(
        messages,
        model=MANAGER_MODEL,
        temperature=0.0,
        top_p=1.0,
        max_tokens=768,
        purpose="Final Answer",
        caller="app.core.agent.manager_agent.synthesize_answer",
    )


def stream_synthesis(messages: list[ChatMessage]):
    """Streaming final manager synthesis."""
    return stream_chat(
        messages,
        model=MANAGER_MODEL,
        temperature=0.0,
        top_p=1.0,
        max_tokens=768,
        purpose="Final Answer Stream",
        caller="app.core.agent.manager_agent.stream_synthesis",
    )


__all__ = [
    "MANAGER_ROUTER_SYSTEM",
    "MANAGER_SYNTHESIZER_SYSTEM",
    "ManagerPlan",
    "manager_plan",
    "manager_synthesizer_messages",
    "synthesize_answer",
    "stream_synthesis",
]
