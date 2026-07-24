import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlparse

import feedparser
import trafilatura
from googlenewsdecoder import gnewsdecoder

ALLOWED_DOMAINS = {
    "bisnis.com",
    "cnbcindonesia.com",
    "kontan.co.id",
    "antaranews.com",
    "bloombergtechnoz.com",
    "idxchannel.com",
}


@dataclass
class Article:
    stock_code: str
    title: str
    url: str
    source: str
    published: str
    text: str


def _root_domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    for domain in ALLOWED_DOMAINS:
        if host == domain or host.endswith(f".{domain}"):
            return domain
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _content_fingerprint(article: "Article") -> str:
    text = f"{article.title} {article.text[:500]}".lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()[:220]


def _is_allowed_source(entry: dict) -> bool:
    src_href = entry.get("source", {}).get("href", "").lower()
    return any(domain in src_href for domain in ALLOWED_DOMAINS)


def _decode_google_url(google_url: str) -> str | None:
    try:
        result = gnewsdecoder(google_url)
        if result.get("status"):
            return result["decoded_url"]
    except Exception:
        pass
    return None


def _extract_text(url: str) -> str | None:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return trafilatura.extract(downloaded, include_comments=False, include_tables=False)


def _crawl_single_query(
    query: str,
    stock_code: str,
    max_articles: int,
    diagnostics: dict[str, int] | None = None,
    lock: Lock | None = None,
) -> list[Article]:
    """Crawl satu RSS query Google News, kembalikan artikel yang lolos filter sumber."""
    articles: list[Article] = []
    rss_url = (
        f"https://news.google.com/rss/search"
        f"?q={query.replace(' ', '+')}&hl=id&gl=ID&ceid=ID:id"
    )
    feed = feedparser.parse(rss_url)

    for entry in feed.entries:
        if diagnostics is not None:
            with lock or Lock():
                diagnostics["feed_entries"] = diagnostics.get("feed_entries", 0) + 1
        if len(articles) >= max_articles:
            break
        if not _is_allowed_source(entry):
            if diagnostics is not None:
                with lock or Lock():
                    diagnostics["source_rejected"] = diagnostics.get("source_rejected", 0) + 1
            continue
        real_url = _decode_google_url(entry.link)
        if not real_url:
            if diagnostics is not None:
                with lock or Lock():
                    diagnostics["decode_failed"] = diagnostics.get("decode_failed", 0) + 1
            continue
        text = _extract_text(real_url)
        if not text:
            if diagnostics is not None:
                with lock or Lock():
                    diagnostics["parse_failed"] = diagnostics.get("parse_failed", 0) + 1
            continue
        articles.append(Article(
            stock_code=stock_code,
            title=entry.get("title", ""),
            url=real_url,
            source=_root_domain(real_url),
            published=entry.get("published", ""),
            text=text,
        ))
        if diagnostics is not None:
            with lock or Lock():
                diagnostics["articles_extracted"] = diagnostics.get("articles_extracted", 0) + 1
        time.sleep(0.5)

    return articles


def crawl_by_keywords(
    keywords: list[str],
    stock_code: str,
    max_total: int = 6,
    diagnostics: dict[str, int] | None = None,
) -> list[Article]:
    """Crawl Google News secara paralel untuk beberapa keyword, deduplikasi per URL.

    Args:
        keywords  : list query pencarian (output dari extract_search_keywords)
        stock_code: kode saham untuk tagging artikel
        max_total : batas total artikel yang dikembalikan

    Returns:
        list Article yang sudah dideduplikasi
    """
    if not keywords:
        return []

    max_per_query = max(2, max_total // len(keywords))
    seen_urls: set[str] = set()
    seen_content: set[str] = set()
    all_articles: list[Article] = []
    diagnostics_lock = Lock()

    with ThreadPoolExecutor(max_workers=min(3, len(keywords))) as executor:
        futures = {
            executor.submit(
                _crawl_single_query,
                kw,
                stock_code,
                max_per_query,
                diagnostics,
                diagnostics_lock,
            ): kw
            for kw in keywords
        }
        for future in as_completed(futures):
            kw = futures[future]
            try:
                for art in future.result():
                    fingerprint = _content_fingerprint(art)
                    if art.url not in seen_urls and fingerprint not in seen_content:
                        seen_urls.add(art.url)
                        seen_content.add(fingerprint)
                        all_articles.append(art)
                    elif diagnostics is not None:
                        with diagnostics_lock:
                            diagnostics["deduplicated"] = diagnostics.get("deduplicated", 0) + 1
            except Exception as exc:
                print(f"[crawl] ERROR keyword '{kw}': {exc}")

    result = all_articles[:max_total]
    if diagnostics is not None:
        diagnostics["articles_returned"] = len(result)
        diagnostics["queries"] = len(keywords)
    print(f"[{stock_code}] {len(result)} artikel unik dari {len(keywords)} keyword")
    return result


def crawl_news(stock_codes: list[str], max_per_code: int = 5) -> dict[str, list[Article]]:
    """Crawl Google News RSS per kode saham, filter sumber terpercaya.

    Args:
        stock_codes: list kode saham IDX, contoh ["BBCA", "TLKM"]
        max_per_code: batas artikel per kode saham

    Returns:
        dict {kode_saham: [Article, ...]}
    """
    results: dict[str, list[Article]] = {}

    for code in stock_codes:
        articles: list[Article] = []
        rss_url = (
            f"https://news.google.com/rss/search"
            f"?q={code}+saham&hl=id&gl=ID&ceid=ID:id"
        )
        feed = feedparser.parse(rss_url)

        for entry in feed.entries:
            if len(articles) >= max_per_code:
                break

            if not _is_allowed_source(entry):
                continue

            real_url = _decode_google_url(entry.link)
            if not real_url:
                continue

            text = _extract_text(real_url)
            if not text:
                continue

            articles.append(
                Article(
                    stock_code=code,
                    title=entry.get("title", ""),
                    url=real_url,
                    source=_root_domain(real_url),
                    published=entry.get("published", ""),
                    text=text,
                )
            )
            time.sleep(1.0)

        results[code] = articles
        print(f"[{code}] {len(articles)} artikel ditemukan")

    return results
