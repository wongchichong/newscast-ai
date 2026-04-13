"""
test_top10.py — Fetch top 10 news headlines and generate a newscast video for each.
Uses BBC News RSS feed (no API key needed for fetching headlines).
"""

import sys
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

import requests

sys.path.insert(0, str(Path(__file__).parent / "tools"))

# ── Config ────────────────────────────────────────────────────────────────────

RSS_FEEDS = [
    ("BBC World News",    "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Top Stories",   "http://feeds.bbci.co.uk/news/rss.xml"),
    ("Reuters",           "https://feeds.reuters.com/reuters/topNews"),
    ("AP News",           "https://rsshub.app/apnews/topics/ap-top-news"),
]

MAX_ARTICLES   = 10
VIDEO_DURATION = 60    # seconds per video
VOICE          = "male_us"

OUTPUT_DIR = Path(__file__).parent / "output"
RESULTS_FILE = Path(__file__).parent / "output" / "top10_results.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_top_headlines(max_items: int = 10) -> list[dict]:
    """Try each RSS feed until we get enough headlines."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewscastBot/1.0)"}
    articles = []

    for feed_name, feed_url in RSS_FEEDS:
        if len(articles) >= max_items:
            break
        try:
            print(f"  Trying {feed_name} RSS...")
            resp = requests.get(feed_url, headers=headers, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)

            # Handle both RSS and Atom
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for item in items:
                title = (item.findtext("title") or
                         item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
                link  = (item.findtext("link") or
                         item.findtext("{http://www.w3.org/2005/Atom}link") or "").strip()
                desc  = (item.findtext("description") or
                         item.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()

                # Some feeds put the URL in link element's 'href' attribute
                if not link:
                    link_el = item.find("{http://www.w3.org/2005/Atom}link")
                    if link_el is not None:
                        link = link_el.get("href", "")

                if title and link and link.startswith("http"):
                    articles.append({
                        "title": title,
                        "url": link,
                        "description": desc[:200],
                        "source": feed_name,
                    })
                if len(articles) >= max_items:
                    break

            print(f"    Got {len(articles)} so far")
        except Exception as e:
            print(f"    Feed failed: {e}")

    return articles[:max_items]


def print_banner(text: str):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import os

    # Detect provider (auto-detects gemini-cli fallback if no key set)
    from summarizer import get_default_provider
    try:
        provider = get_default_provider()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print(f"Using LLM provider: {provider}")

    # Fetch headlines
    print_banner("Fetching Top 10 News Headlines")
    articles = fetch_top_headlines(MAX_ARTICLES)

    if not articles:
        print("ERROR: Could not fetch any headlines. Check internet connection.")
        sys.exit(1)

    print(f"\nFetched {len(articles)} articles:")
    for i, a in enumerate(articles, 1):
        print(f"  {i:2d}. [{a['source']}] {a['title'][:70]}")

    # Run pipeline for each
    from pipeline import run_pipeline

    OUTPUT_DIR.mkdir(exist_ok=True)
    results = []
    start_time = time.time()

    for i, article in enumerate(articles, 1):
        print_banner(f"Article {i}/{len(articles)}: {article['title'][:50]}")
        print(f"URL: {article['url']}")

        job_start = time.time()
        try:
            result = run_pipeline(
                url=article["url"],
                duration_seconds=VIDEO_DURATION,
                voice_key=VOICE,
                output_filename=f"news_{i:02d}_{datetime.now().strftime('%H%M%S')}.mp4",
                llm_provider=provider,
            )
            elapsed = time.time() - job_start
            status = "ok" if result.get("output_video") else "partial"
            results.append({
                "rank": i,
                "title": article["title"],
                "url": article["url"],
                "source": article["source"],
                "status": status,
                "output_video": result.get("output_video"),
                "headline": result.get("script", {}).get("headline", ""),
                "elapsed_sec": round(elapsed, 1),
                "stages": result.get("stages", {}),
            })
            print(f"  Done in {elapsed:.0f}s → {result.get('output_video','(no output)')}")

        except Exception as e:
            elapsed = time.time() - job_start
            print(f"  FAILED: {e}")
            results.append({
                "rank": i,
                "title": article["title"],
                "url": article["url"],
                "source": article["source"],
                "status": "error",
                "error": str(e),
                "elapsed_sec": round(elapsed, 1),
            })

        # Save progress after each article
        with open(RESULTS_FILE, "w") as f:
            json.dump(results, f, indent=2)

    # Summary
    total_elapsed = time.time() - start_time
    ok_count = sum(1 for r in results if r["status"] == "ok")
    partial_count = sum(1 for r in results if r["status"] == "partial")
    error_count = sum(1 for r in results if r["status"] == "error")

    print_banner("RESULTS SUMMARY")
    print(f"Total time:  {total_elapsed/60:.1f} min")
    print(f"Success:     {ok_count}/{len(results)}")
    print(f"Partial:     {partial_count}/{len(results)}")
    print(f"Failed:      {error_count}/{len(results)}")
    print(f"\nResults saved to: {RESULTS_FILE}")
    print(f"\nGenerated videos:")
    for r in results:
        vid = r.get("output_video") or "(none)"
        mark = "✓" if r["status"] == "ok" else ("~" if r["status"] == "partial" else "✗")
        print(f"  {mark} [{r['rank']:2d}] {r['title'][:55]}")
        print(f"       → {vid}")


if __name__ == "__main__":
    main()
