"""
news_search.py — Search news by category/topic across multiple countries.

Uses Google News RSS (free, no API key). Scrapes article text for top results.
"""

import re
import time
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
import trafilatura

# ── Country config ────────────────────────────────────────────────────────────

COUNTRIES = {
    "us": {"name": "United States", "ceid": "US:en",    "hl": "en-US"},
    "uk": {"name": "United Kingdom", "ceid": "GB:en",    "hl": "en-GB"},
    "cn": {"name": "China",          "ceid": "CN:en",    "hl": "en"},
    "jp": {"name": "Japan",          "ceid": "JP:en",    "hl": "en"},
    "de": {"name": "Germany",        "ceid": "DE:en",    "hl": "en"},
    "fr": {"name": "France",         "ceid": "FR:en",    "hl": "en"},
    "au": {"name": "Australia",      "ceid": "AU:en",    "hl": "en-AU"},
    "in": {"name": "India",          "ceid": "IN:en",    "hl": "en-IN"},
    "ru": {"name": "Russia",         "ceid": "RU:en",    "hl": "en"},
    "br": {"name": "Brazil",         "ceid": "BR:en",    "hl": "en"},
    "kr": {"name": "South Korea",    "ceid": "KR:en",    "hl": "en"},
    "sg": {"name": "Singapore",      "ceid": "SG:en",    "hl": "en"},
}

DEFAULT_COUNTRIES = ["us", "uk", "cn", "jp", "de", "fr", "au", "in"]

GNEWS_RSS = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={cc}&ceid={ceid}"
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; NewscastBot/1.0)"}


# ── RSS fetch ─────────────────────────────────────────────────────────────────

def _fetch_rss(url: str, timeout: int = 10) -> list[dict]:
    """Fetch Google News RSS and return list of {title, url, source, published}."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            src_el = item.find("source")
            source = src_el.text.strip() if src_el is not None else ""
            if title and link:
                items.append({"title": title, "url": link, "source": source, "published": pub})
        return items
    except Exception as e:
        print(f"  [search] RSS fetch failed: {e}")
        return []


def _scrape_article(url: str, timeout: int = 12) -> dict:
    """
    Scrape article text from URL.
    First tries fast requests+trafilatura; if text is too short (<200 chars)
    falls back to Playwright (renders JS, handles SPAs and soft-paywalls).
    Returns {text, title, html}.
    """
    result = _scrape_requests(url, timeout)
    if len(result.get("text", "")) >= 200:
        return result
    print(f"    (requests got {len(result.get('text',''))} chars — trying Playwright...)")
    pw_result = _scrape_playwright(url, timeout=max(timeout, 20))
    # Merge: prefer Playwright text/title if better, keep requests html as fallback
    if len(pw_result.get("text", "")) > len(result.get("text", "")):
        result["text"]  = pw_result["text"]
        result["title"] = pw_result.get("title") or result.get("title", "")
        result["html"]  = pw_result.get("html")  or result.get("html", "")
    return result


def _scrape_requests(url: str, timeout: int = 12) -> dict:
    """Fast path: requests + trafilatura (no JS rendering)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        for sel in ["h1", 'meta[property="og:title"]', "title"]:
            tag = soup.select_one(sel)
            if tag:
                title = tag.get("content", "") or tag.get_text(strip=True)
                if title:
                    break
        return {"text": text, "title": title, "html": html, "url": url}
    except Exception as e:
        return {"text": "", "title": "", "html": "", "url": url, "error": str(e)}


def _scrape_playwright(url: str, timeout: int = 20) -> dict:
    """
    Playwright-based scraper: renders JS, waits for content, extracts text.
    Handles SPAs, lazy-loaded articles, and cookie consent banners.
    """
    import asyncio

    async def _run(url: str, timeout_ms: int) -> dict:
        from playwright.async_api import async_playwright
        CHROMIUM_ARGS = [
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-extensions", "--mute-audio",
        ]
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    ignore_https_errors=True,
                )
                page = await context.new_page()

                # Block ads/trackers to speed up load
                await page.route("**/*", lambda r: r.abort()
                    if any(b in r.request.url for b in [
                        "doubleclick", "googlesyndication", "googletagmanager",
                        "google-analytics", "facebook.com/tr", "connect.facebook",
                        "ads.", "adservice", "analytics.", "tracking.",
                    ]) else r.continue_())

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 1000)
                except Exception:
                    pass  # partial load is fine

                # Wait for main content to appear
                await page.wait_for_timeout(2500)

                # Dismiss cookie banners
                for sel in [
                    "button:has-text('Accept all')", "button:has-text('Accept All')",
                    "button:has-text('Accept cookies')", "button:has-text('I agree')",
                    "button:has-text('Agree')", "button:has-text('OK')",
                    "#onetrust-accept-btn-handler", ".cc-accept",
                ]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=400):
                            await btn.click(timeout=400)
                            await page.wait_for_timeout(300)
                            break
                    except Exception:
                        pass

                # Extra wait after banner dismiss
                await page.wait_for_timeout(1000)

                # Get rendered HTML then extract with trafilatura
                html = await page.content()

                # Also extract text directly from DOM for speed
                text_from_dom = await page.evaluate("""() => {
                    // Try article-specific selectors first
                    const selectors = [
                        'article', '[role="main"]', 'main',
                        '.article-body', '.article__body', '.story-body',
                        '.post-content', '.entry-content', '.content-body',
                        '#article-body', '#story-body', '#main-content',
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText && el.innerText.trim().length > 200) {
                            return el.innerText.trim();
                        }
                    }
                    // Fall back to body text, strip nav/footer noise
                    const body = document.body;
                    if (!body) return '';
                    // Remove nav, footer, aside, script, style
                    const clone = body.cloneNode(true);
                    for (const tag of ['nav','footer','aside','script','style','header',
                                       '.ad','[class*="cookie"]','[class*="banner"]']) {
                        clone.querySelectorAll(tag).forEach(n => n.remove());
                    }
                    return clone.innerText.trim().slice(0, 8000);
                }""")

                # Also try trafilatura on the rendered HTML
                text_trafilatura = trafilatura.extract(
                    html, include_comments=False, include_tables=False
                ) or ""

                # Use whichever gives more text
                text = text_trafilatura if len(text_trafilatura) > len(text_from_dom) else text_from_dom

                # Get title
                title = await page.title()
                # Prefer og:title
                try:
                    og = await page.locator('meta[property="og:title"]').get_attribute("content", timeout=500)
                    if og and og.strip():
                        title = og.strip()
                except Exception:
                    pass

                await context.close()
                await browser.close()
                return {"text": text, "title": title, "html": html, "url": url}
        except Exception as e:
            return {"text": "", "title": "", "html": "", "url": url, "error": str(e)}

    try:
        return asyncio.run(_run(url, timeout))
    except Exception as e:
        return {"text": "", "title": "", "html": "", "url": url, "error": str(e)}


# ── Public search ─────────────────────────────────────────────────────────────

def search_topic(
    query: str,
    countries: list[str] = None,
    max_per_country: int = 2,
    scrape_full_text: bool = True,
    job_dir: Path = None,
) -> list[dict]:
    """
    Search for query across multiple countries via Google News RSS.
    Returns list of article dicts: {country, country_name, title, url, source, text, html}
    """
    if countries is None:
        countries = DEFAULT_COUNTRIES

    results = []
    seen_urls = set()

    for cc in countries:
        cfg = COUNTRIES.get(cc.lower())
        if not cfg:
            print(f"  [search] Unknown country code: {cc}")
            continue

        rss_url = GNEWS_RSS.format(
            query=requests.utils.quote(query),
            hl=cfg["hl"],
            cc=cc.upper(),
            ceid=cfg["ceid"],
        )
        print(f"  [search] {cfg['name']}: searching '{query}'...")
        items = _fetch_rss(rss_url)

        count = 0
        for item in items[:6]:  # check up to 6, take max_per_country good ones
            url = item["url"]
            if url in seen_urls:
                continue

            # Skip Google's own aggregator links (redirect URLs)
            if "news.google.com/rss/articles" in url:
                # Try to resolve the redirect
                try:
                    r = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=8)
                    url = r.url
                except Exception:
                    continue

            if url in seen_urls:
                continue
            seen_urls.add(url)

            article = {"country": cc, "country_name": cfg["name"],
                       "title": item["title"], "url": url,
                       "source": item["source"], "published": item["published"],
                       "text": "", "html": ""}

            if scrape_full_text:
                print(f"    Scraping: {item['title'][:60]}...")
                scraped = _scrape_article(url)
                article["text"]  = scraped.get("text", "")
                article["html"]  = scraped.get("html", "")
                if not article["text"] or len(article["text"]) < 100:
                    print(f"    (skipped — no text)")
                    continue
                time.sleep(0.3)  # polite crawl delay

            results.append(article)
            count += 1
            if count >= max_per_country:
                break

        print(f"    → {count} article(s) from {cfg['name']}")

    # Save to job_dir if provided
    if job_dir:
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        safe = [
            {k: v for k, v in a.items() if k != "html"}
            for a in results
        ]
        with open(Path(job_dir) / "search_results.json", "w") as f:
            json.dump(safe, f, indent=2)

    print(f"  [search] Total: {len(results)} articles from {len(set(a['country'] for a in results))} countries")
    return results


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "artificial intelligence"
    cc = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    articles = search_topic(q, countries=cc, max_per_country=1, scrape_full_text=False)
    for a in articles:
        print(f"[{a['country_name']}] {a['title'][:70]} | {a['source']}")
