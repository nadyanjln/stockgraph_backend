"""
Test multi-tenant graph builder.

Jalankan FalkorDB dulu:
    docker run -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
"""
import sys
sys.path.insert(0, "src")

from agents.financial_fetcher import fetch_multiple
from agents.llm_extractor import extract_all
from agents.news_crawler import crawl_news
from agents.relevance_checker import filter_results
from graph.graph_builder import build_graph_multi_tenant, list_year_graphs, validate_graph

STOCKS = ["BBCA", "BBRI"]
THRESHOLD = 0.6

print("=== [1/4] Crawling news ===")
articles = crawl_news(STOCKS, max_per_code=2)

print("\n=== [2/4] Fetching financial data (3-tahun historical) ===")
financial = fetch_multiple(STOCKS)
for code, data in financial.items():
    years = [s.year for s in data.historical if s.year > 0]
    print(f"  {code}: years={years}")

print("\n=== [3/4] LLM Extraction + Relevance Check ===")
extractions = extract_all(articles, financial)
company_names = {code: d.company_name for code, d in financial.items()}
checked = filter_results(extractions, company_names=company_names, threshold=THRESHOLD)

print("\n=== [4/4] Building Multi-Tenant Knowledge Graph ===")
per_year_stats = build_graph_multi_tenant(checked, articles, financial)

for year, stats in per_year_stats.items():
    print(f"\n[{stats.graph_name}]")
    print(f"  Nodes baru   : {stats.nodes_created}")
    print(f"  Edges baru   : {stats.edges_created}")
    print(f"  Errors       : {stats.errors}")
    if stats.error_messages[:3]:
        for msg in stats.error_messages[:3]:
            print(f"    - {msg}")

print("\n=== Daftar Year Graphs ===")
years = list_year_graphs()
print(f"Years tersedia: {years}")

if years:
    target = years[-1]
    print(f"\n=== Validasi Graph FY{target} ===")
    v = validate_graph(target)
    print(f"Total nodes : {v['total_nodes']}")
    print(f"Total edges : {v['total_edges']}")
    print("\nNodes per label:")
    for t, c in v["nodes"].items():
        if c > 0:
            print(f"  {t:<18}: {c}")
    print("\nEdges per type:")
    for t, c in v["edges"].items():
        print(f"  {t:<18}: {c}")
    print(f"\nStock nodes: {[s[0] for s in v['stocks']]}")
