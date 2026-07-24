import sys
sys.path.insert(0, "src")

from agents.news_crawler import crawl_news
from agents.financial_fetcher import fetch_multiple
from agents.llm_extractor import extract_all

STOCKS = ["BBCA", "BBRI"]

print("=== Crawling news ===")
articles = crawl_news(STOCKS, max_per_code=2)

print("\n=== Fetching financial data ===")
financial = fetch_multiple(STOCKS)

print("\n=== Running LLM Extraction ===")
results = extract_all(articles, financial)

print("\n" + "="*70)
for code, extractions in results.items():
    print(f"\n{'='*30} {code} {'='*30}")
    for ext in extractions:
        print(f"\n[{ext.source_type.upper()}] {ext.source_ref[:80]}")
        print(f"  Entitas ({len(ext.entities)}):")
        for e in ext.entities:
            attrs = ", ".join(f"{k}={v}" for k, v in e.attributes.items() if v)
            print(f"    - [{e.type}] {e.name}" + (f" ({attrs})" if attrs else ""))
        print(f"  Relasi ({len(ext.relations)}):")
        for r in ext.relations:
            print(f"    - {r.source} --[{r.type}]--> {r.target}: {r.description}")
