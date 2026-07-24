import sys
sys.path.insert(0, "src")

from agents.news_crawler import crawl_news

results = crawl_news(["BBCA"], max_per_code=3)

for code, articles in results.items():
    print(f"\n=== {code} ({len(articles)} artikel) ===")
    for i, art in enumerate(articles, 1):
        print(f"\n[{i}] {art.source} | {art.published}")
        print(f"    Judul : {art.title}")
        print(f"    URL   : {art.url}")
        print(f"    Teks  : {art.text[:300]}...")
        print("-" * 60)
