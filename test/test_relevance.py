import sys
sys.path.insert(0, "src")

from agents.news_crawler import crawl_news
from agents.financial_fetcher import fetch_multiple
from agents.llm_extractor import extract_all
from agents.relevance_checker import filter_results, get_passed

STOCKS = ["BBCA", "BBRI"]
THRESHOLD = 0.6

print("=== Crawling news ===")
articles = crawl_news(STOCKS, max_per_code=2)

print("\n=== Fetching financial data ===")
financial = fetch_multiple(STOCKS)

print("\n=== LLM Extraction ===")
extractions = extract_all(articles, financial)

# Bangun mapping nama perusahaan dari financial data
company_names = {code: d.company_name for code, d in financial.items()}

print(f"\n=== Relevance Check (threshold={THRESHOLD}) ===")
checked = filter_results(extractions, company_names=company_names, threshold=THRESHOLD)
passed = get_passed(checked)

# Ringkasan
print("\n" + "="*70)
print("RINGKASAN HASIL:")
for code in STOCKS:
    total = len(checked.get(code, []))
    lolos = len(passed.get(code, []))
    print(f"  {code}: {lolos}/{total} lolos threshold {THRESHOLD}")

# Detail per kode saham
print("\nDETAIL KONTEN YANG LOLOS:")
for code, items in passed.items():
    if not items:
        print(f"\n[{code}] — tidak ada konten yang lolos")
        continue
    print(f"\n{'='*30} {code} {'='*30}")
    for item in items:
        ext = item.extraction
        s = item.score
        print(f"\n  [{ext.source_type.upper()}] {ext.source_ref[:80]}")
        print(f"  Skor: relevansi={s.relevance:.2f}, kepercayaan={s.confidence:.2f}")
        print(f"  Alasan: {s.reason}")
        print(f"  Entitas: {[e.name for e in ext.entities]}")
