"""
GraphSchema untuk analisis saham BEI berbasis GraphRAG-SDK FalkorDB.

Topik utama:
  - Berita pasar (Bisnis.com, CNBC Indonesia) — Entity yang muncul + relasi AFFECTS ke saham
  - Laporan keuangan IDX — metrik fundamental tahunan + relasi REPORTS_FINANCIAL

Dipakai oleh:
  - app/services/database/graph_builder.py - membuat dokumen ingestion per tahun
  - app/services/database/graphrag_engine.py - ingestion dan query via GraphRAG-SDK
"""

from __future__ import annotations

from graphrag_sdk import EntityType, GraphSchema, RelationType


def build_bei_schema() -> GraphSchema:
    """
    Bangun GraphSchema khusus analisis saham BEI.

    Entity types fokus pada domain perbankan + ekonomi makro:
      Stock, Company, Person, Policy, Event, FinancialMetric, NewsArticle, Sector
    """
    entities = [
        EntityType(
            label="Stock",
            description=(
                "Saham emiten yang tercatat di Bursa Efek Indonesia (BEI). "
                "Direpresentasikan dengan kode 4 huruf seperti BBCA, BBRI, BMRI."
            ),
        ),
        EntityType(
            label="Company",
            description=(
                "Perusahaan publik penerbit saham, BUMN, atau lembaga keuangan terkait emiten. "
                "Contoh: Bank Central Asia, Bank Mandiri, Otoritas Jasa Keuangan."
            ),
        ),
        EntityType(
            label="Person",
            description=(
                "Individu yang relevan dengan emiten: direktur, komisaris, pejabat regulator, "
                "atau analis pasar yang dikutip dalam berita."
            ),
        ),
        EntityType(
            label="Policy",
            description=(
                "Kebijakan, regulasi, atau program pemerintah yang berdampak ke pasar saham. "
                "Contoh: BI Rate, GWM, POJK, Kenaikan PPN, Kebijakan moneter The Fed."
            ),
        ),
        EntityType(
            label="Event",
            description=(
                "Peristiwa korporasi atau ekonomi: RUPS, IPO, akuisisi, right issue, dividen, "
                "krisis ekonomi, rilis laporan keuangan kuartalan."
            ),
        ),
        EntityType(
            label="FinancialMetric",
            description=(
                "Metrik fundamental tahunan emiten: revenue, net_profit, total_assets, "
                "total_equity, EPS, ROE, PER, PBV. Tiap metrik terikat ke satu Stock + satu tahun."
            ),
        ),
        EntityType(
            label="NewsArticle",
            description=(
                "Artikel berita dari sumber terpercaya (Bisnis.com, CNBC Indonesia). "
                "Sumber konteks naratif untuk analisis qualitative."
            ),
        ),
        EntityType(
            label="Sector",
            description=(
                "Sektor industri pasar saham: perbankan, telekomunikasi, otomotif, consumer goods. "
                "Dipakai untuk analisis sektoral."
            ),
        ),
    ]

    relations = [
        RelationType(
            label="MANAGES",
            description="Person menjabat di Company sebagai direktur/komisaris/pejabat.",
            patterns=[("Person", "Company")],
        ),
        RelationType(
            label="ISSUES",
            description="Company atau lembaga regulator menerbitkan suatu Policy.",
            patterns=[("Company", "Policy")],
        ),
        RelationType(
            label="AFFECTS",
            description="Event atau Policy memberi dampak pada Stock atau sektor (positif/negatif).",
            patterns=[
                ("Event", "Stock"),
                ("Policy", "Stock"),
                ("Event", "Sector"),
                ("Policy", "Sector"),
                ("Policy", "Company"),
            ],
        ),
        RelationType(
            label="REPORTS_FINANCIAL",
            description="Stock memiliki FinancialMetric pada tahun tertentu (laporan keuangan IDX).",
            patterns=[("Stock", "FinancialMetric")],
        ),
        RelationType(
            label="MENTIONS",
            description="NewsArticle menyebutkan / membahas suatu entitas.",
            patterns=[
                ("NewsArticle", "Stock"),
                ("NewsArticle", "Company"),
                ("NewsArticle", "Person"),
                ("NewsArticle", "Policy"),
                ("NewsArticle", "Event"),
            ],
        ),
        RelationType(
            label="COMPETES_WITH",
            description="Dua Stock bersaing di sektor yang sama.",
            patterns=[("Stock", "Stock")],
        ),
        RelationType(
            label="BELONGS_TO",
            description="Stock atau Company termasuk dalam suatu Sector.",
            patterns=[("Stock", "Sector"), ("Company", "Sector")],
        ),
        RelationType(
            label="REPRESENTS",
            description="Stock merepresentasikan saham yang diterbitkan oleh Company.",
            patterns=[("Stock", "Company")],
        ),
    ]

    return GraphSchema(entities=entities, relations=relations)


# Singleton — di-cache agar import berikutnya tidak rebuild
BEI_SCHEMA: GraphSchema = build_bei_schema()
