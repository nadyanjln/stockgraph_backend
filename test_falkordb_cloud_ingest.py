"""Script pengujian koneksi & ingest dokumen ke FalkorDB Cloud."""

from __future__ import annotations

import os
from dotenv import load_dotenv
from falkordb import FalkorDB
from app.services.database.graph_builder import IngestDocument, validate_graph
from app.services.database.graphrag_engine import GraphRAGEngine

load_dotenv()

def test_ingest():
    host = os.getenv("FALKORDB_HOST")
    port = int(os.getenv("FALKORDB_PORT", 6379))
    username = os.getenv("FALKORDB_USERNAME")
    password = os.getenv("FALKORDB_PASSWORD")

    print(f"📡 Menghubungkan ke FalkorDB Cloud: {host}:{port}...")
    client = FalkorDB(host=host, port=port, username=username, password=password)
    print("✅ Berhasil terhubung! Graphs yang ada:", client.list_graphs())

    # Dokumen sampel untuk pengujian ingest (BBCA, BBRI, TLKM)
    sample_docs_2024 = [
        IngestDocument(
            year=2024,
            document_id="idx:BBCA:2024",
            text=(
                "Jenis dokumen: Laporan Keuangan BEI\n"
                "Tahun: 2024\n"
                "Kode Saham: BBCA\n"
                "Nama Perusahaan: PT Bank Central Asia Tbk\n"
                "Sektor: Perbankan\n"
                "Laba Bersih: Rp 48.6 Triliun (Tumbuh 12% YoY)\n"
                "Pendapatan Bunga Bersih: Rp 75.2 Triliun\n"
                "Kredit yang Disalurkan: Rp 810 Triliun\n"
                "NPL Gross: 1.9%\n"
                "Catatan: Kinerja keuangan BBCA sangat solid didukung efisiensi operasional dan dana murah CASA."
            ),
        ),
        IngestDocument(
            year=2024,
            document_id="idx:BBRI:2024",
            text=(
                "Jenis dokumen: Laporan Keuangan BEI\n"
                "Tahun: 2024\n"
                "Kode Saham: BBRI\n"
                "Nama Perusahaan: PT Bank Rakyat Indonesia Tbk\n"
                "Sektor: Perbankan\n"
                "Laba Bersih: Rp 60.4 Triliun\n"
                "Kredit Mikro: Rp 620 Triliun\n"
                "Catatan: BRI mempertahankan kepemimpinan di segmen UMKM dan mikro dengan penyaluran kredit yang prudent."
            ),
        ),
        IngestDocument(
            year=2024,
            document_id="news:TLKM:2024:01",
            text=(
                "Jenis dokumen: Berita Pasar Modal\n"
                "Tahun: 2024\n"
                "Kode Saham: TLKM\n"
                "Nama Perusahaan: PT Telkom Indonesia Tbk\n"
                "Sektor: Telekomunikasi\n"
                "Judul: Telkom Memperluas Ekspansi Data Center dan Layanan IndiHome\n"
                "Ringkasan: Telkom Indonesia terus memperkuat portofolio bisnis digital dan pusat data (data center) untuk mendukung transformasi digital nasional."
            ),
        ),
    ]

    print("\n🚀 Memulai proses ingest dokumen sampel ke FalkorDB Cloud...")
    import asyncio

    async def run_ingest():
        async with GraphRAGEngine(
            host=host,
            port=port,
            password=password,
            username=username,
        ) as engine:
            stats = await engine.ingest_documents(2024, sample_docs_2024)
            print("📊 Ingest Stats (Tahun 2024):", stats)

    asyncio.run(run_ingest())

    print("\n🔍 Memverifikasi graf yang terbentuk di FalkorDB Cloud...")
    print("Daftar Graphs terkini:", client.list_graphs())
    val = validate_graph(2024, host=host, port=port, password=password, username=username)
    print("Status Graf 2024:", val)
    print("\n🎉 Pengujian Ingest ke FalkorDB Cloud Selesai & Berhasil!")

if __name__ == "__main__":
    test_ingest()
