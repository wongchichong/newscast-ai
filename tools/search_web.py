"""
search_web.py — Multi-country, multi-source web search for newscast pipeline v2.

Searches a topic across all configured countries using:
  1. Google News RSS (per-country locale settings)
  2. Native/official news agency RSS feeds
  3. DuckDuckGo / Bing web search as fallback

Reuses COUNTRY config from news_search.py for consistency.
"""

import re
import time
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import trafilatura

# ── Reuse country config from news_search ─────────────────────────────────────

COUNTRIES = {
    "us": {
        "name": "United States",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "US:en",
        "gnews_hl": "en-US",
    },
    "uk": {
        "name": "United Kingdom",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "GB:en",
        "gnews_hl": "en-GB",
    },
    "cn": {
        "name": "China",
        "language": "zh",
        "lang_name": "Chinese",
        "gnews_ceid": "CN:zh-Hans",
        "gnews_hl": "zh-CN",
    },
    "jp": {
        "name": "Japan",
        "language": "ja",
        "lang_name": "Japanese",
        "gnews_ceid": "JP:ja",
        "gnews_hl": "ja",
    },
    "de": {
        "name": "Germany",
        "language": "de",
        "lang_name": "German",
        "gnews_ceid": "DE:de",
        "gnews_hl": "de",
    },
    "fr": {
        "name": "France",
        "language": "fr",
        "lang_name": "French",
        "gnews_ceid": "FR:fr",
        "gnews_hl": "fr",
    },
    "ru": {
        "name": "Russia",
        "language": "ru",
        "lang_name": "Russian",
        "gnews_ceid": "RU:ru",
        "gnews_hl": "ru",
    },
    "in": {
        "name": "India",
        "language": "hi",
        "lang_name": "Hindi",
        "gnews_ceid": "IN:hi",
        "gnews_hl": "hi",
    },
    "br": {
        "name": "Brazil",
        "language": "pt",
        "lang_name": "Portuguese",
        "gnews_ceid": "BR:pt",
        "gnews_hl": "pt-BR",
    },
    "kr": {
        "name": "South Korea",
        "language": "ko",
        "lang_name": "Korean",
        "gnews_ceid": "KR:ko",
        "gnews_hl": "ko",
    },
    "au": {
        "name": "Australia",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "AU:en",
        "gnews_hl": "en-AU",
    },
    "ca": {
        "name": "Canada",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "CA:en",
        "gnews_hl": "en-CA",
    },
}

ALL_COUNTRY_CODES = list(COUNTRIES.keys())


# ── Google News search ────────────────────────────────────────────────────────

def _search_google_news(topic: str, country_code: str, max_results: int = 20) -> list[dict]:
    """
    Search Google News for a topic in a specific country.
    Returns list of {title, url, source, published, snippet}.
    """
    country = COUNTRIES.get(country_code, COUNTRIES["us"])
    ceid = country["gnews_ceid"]
    hl = country["gnews_hl"]

    # Google News RSS search URL
    query = requests.utils.quote(topic)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={query}&hl={hl}&gl={country_code.upper()}&ceid={ceid}"
    )

    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()

        results = []
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            title_el = item.find("title")
            link_el = item.find("link")
            source_el = item.find("source")
            pub_el = item.find("pubDate")

            if title_el is not None and link_el is not None:
                title_text = title_el.text or ""
                # Google News formats title as "Article Title - Source"
                parts = title_text.rsplit(" - ", 1)
                article_title = parts[0] if len(parts) > 1 else title_text
                source = parts[1] if len(parts) > 1 else (source_el.text if source_el is not None else "")

                results.append({
                    "title": article_title.strip(),
                    "url": link_el.text or "",
                    "source": source.strip(),
                    "published": pub_el.text if pub_el is not None else "",
                    "snippet": "",
                    "country": country_code,
                })

            if len(results) >= max_results:
                break

        return results[:max_results]

    except Exception as e:
        print(f"  [gnews] {country_code} search failed: {e}")
        return []


# ── DuckDuckGo web search (fallback / supplement) ─────────────────────────────

def _search_duckduckgo(topic: str, max_results: int = 20) -> list[dict]:
    """
    Search DuckDuckGo for web results.
    Returns list of {title, url, source, snippet}.
    """
    results = []
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            for r in ddgs.text(topic, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "source": r.get("source", ""),
                    "snippet": r.get("body", ""),
                    "country": "global",
                })
        return results
    except ImportError:
        print("  [ddg] duckduckgo-search not installed, skipping")
        return []
    except Exception as e:
        print(f"  [ddg] search failed: {e}")
        return []


# ── Main search function ─────────────────────────────────────────────────────

def search_web(
    topic: str,
    countries: list[str] = None,
    max_per_country: int = 15,
    include_global: bool = True,
) -> dict:
    """
    Search a topic across multiple countries for web articles.

    Args:
        topic: Search query / topic
        countries: List of country codes (default: all configured countries)
        max_per_country: Max results per country
        include_global: Whether to include DuckDuckGo global search

    Returns:
        {
            "articles": [{"title", "url", "source", "country", "published", "snippet"}],
            "total_found": int,
            "countries_searched": list[str],
            "query": str,
        }
    """
    if countries is None:
        countries = ALL_COUNTRY_CODES

    all_articles = []
    countries_searched = []

    # Search per country
    for cc in countries:
        if cc not in COUNTRIES:
            print(f"  [search_web] Unknown country: {cc}, skipping")
            continue

        print(f"  [search_web] Searching {COUNTRIES[cc]['name']} ({cc}) for: {topic}")
        articles = _search_google_news(topic, cc, max_results=max_per_country)
        all_articles.extend(articles)
        countries_searched.append(cc)

        # Small delay to avoid rate limiting
        time.sleep(0.5)

    # Global web search supplement
    if include_global:
        print(f"  [search_web] Global web search for: {topic}")
        global_results = _search_duckduckgo(topic, max_results=max_per_country * 2)
        all_articles.extend(global_results)

    # Deduplicate by URL
    seen_urls = set()
    unique_articles = []
    for a in all_articles:
        url = a.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_articles.append(a)

    result = {
        "articles": unique_articles,
        "total_found": len(unique_articles),
        "countries_searched": countries_searched,
        "query": topic,
        "timestamp": datetime.utcnow().isoformat(),
    }

    print(f"  [search_web] Found {len(unique_articles)} unique articles from {len(countries_searched)} countries")
    return result


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    topic = sys.argv[1] if len(sys.argv) > 1 else "artificial intelligence"
    result = search_web(topic)
    print(json.dumps(result, indent=2, ensure_ascii=False)[:3000])
