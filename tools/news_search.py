"""
news_search.py — Multi-source, multi-language news search across countries.

Strategy per country:
  1. Google News RSS (native language settings)
  2. Native/official news agency RSS feeds (in country's own language)
  3. Playwright scraper for full article text

Articles keep their original language — the LLM handles translation + synthesis.
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
    "us": {
        "name": "United States",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "US:en",
        "gnews_hl": "en-US",
        "native_sources": [
            {"name": "AP News",  "rss": "https://rsshub.app/apnews/topics/ap-top-news"},
            {"name": "Reuters",  "rss": "https://feeds.reuters.com/reuters/topNews"},
            {"name": "NPR",      "rss": "https://feeds.npr.org/1001/rss.xml"},
        ],
    },
    "uk": {
        "name": "United Kingdom",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "GB:en",
        "gnews_hl": "en-GB",
        "native_sources": [
            {"name": "BBC World",    "rss": "http://feeds.bbci.co.uk/news/world/rss.xml"},
            {"name": "The Guardian", "rss": "https://www.theguardian.com/world/rss"},
            {"name": "Sky News",     "rss": "https://feeds.skynews.com/feeds/rss/world.xml"},
        ],
    },
    "cn": {
        "name": "China",
        "language": "zh",
        "lang_name": "Chinese",
        "gnews_ceid": "CN:zh-Hans",
        "gnews_hl": "zh-CN",
        "native_sources": [
            {"name": "Xinhua English",  "rss": "http://www.xinhuanet.com/english/rss/worldrss.xml"},
            {"name": "People's Daily",  "rss": "http://en.people.cn/rss/90777.xml"},
            {"name": "Global Times",    "rss": "https://www.globaltimes.cn/rss/outbrain.xml"},
            {"name": "China Daily",     "rss": "http://www.chinadaily.com.cn/rss/cndy_rss.xml"},
        ],
    },
    "jp": {
        "name": "Japan",
        "language": "ja",
        "lang_name": "Japanese",
        "gnews_ceid": "JP:ja",
        "gnews_hl": "ja",
        "native_sources": [
            {"name": "NHK World",      "rss": "https://www3.nhk.or.jp/rss/news/cat0.xml"},
            {"name": "Japan Times",    "rss": "https://www.japantimes.co.jp/feed/"},
            {"name": "Yomiuri (EN)",   "rss": "https://the-japan-news.com/feed"},
            {"name": "Mainichi",       "rss": "https://mainichi.jp/rss/etc/mainichi-flash.rss"},
        ],
    },
    "de": {
        "name": "Germany",
        "language": "de",
        "lang_name": "German",
        "gnews_ceid": "DE:de",
        "gnews_hl": "de",
        "native_sources": [
            {"name": "Deutsche Welle",  "rss": "https://rss.dw.com/rdf/rss-en-world"},
            {"name": "Der Spiegel",     "rss": "https://www.spiegel.de/schlagzeilen/index.rss"},
            {"name": "Die Zeit",        "rss": "https://newsfeed.zeit.de/index"},
            {"name": "ARD Tagesschau",  "rss": "https://www.tagesschau.de/xml/rss2"},
        ],
    },
    "fr": {
        "name": "France",
        "language": "fr",
        "lang_name": "French",
        "gnews_ceid": "FR:fr",
        "gnews_hl": "fr",
        "native_sources": [
            {"name": "France24",   "rss": "https://www.france24.com/en/rss"},
            {"name": "RFI",        "rss": "https://www.rfi.fr/en/rss"},
            {"name": "Le Monde",   "rss": "https://www.lemonde.fr/rss/une.xml"},
            {"name": "Le Figaro",  "rss": "https://www.lefigaro.fr/rss/figaro_actualites.xml"},
        ],
    },
    "au": {
        "name": "Australia",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "AU:en",
        "gnews_hl": "en-AU",
        "native_sources": [
            {"name": "ABC Australia", "rss": "https://www.abc.net.au/news/feed/51120/rss.xml"},
            {"name": "SMH World",     "rss": "https://www.smh.com.au/rss/world.xml"},
            {"name": "The Australian","rss": "https://www.theaustralian.com.au/feed/latest"},
        ],
    },
    "in": {
        "name": "India",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "IN:en",
        "gnews_hl": "en-IN",
        "native_sources": [
            {"name": "NDTV",          "rss": "https://feeds.feedburner.com/ndtvnews-world-news"},
            {"name": "The Hindu",     "rss": "https://www.thehindu.com/news/international/?service=rss"},
            {"name": "Times of India","rss": "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms"},
            {"name": "Hindustan Times","rss": "https://www.hindustantimes.com/feeds/rss/world/rssfeed.xml"},
        ],
    },
    "ru": {
        "name": "Russia",
        "language": "ru",
        "lang_name": "Russian",
        "gnews_ceid": "RU:ru",
        "gnews_hl": "ru",
        "native_sources": [
            {"name": "TASS English", "rss": "https://tass.com/rss/v2.xml"},
            {"name": "Interfax",     "rss": "https://interfax.com/newsinf.asp?id=673&type=1"},
        ],
    },
    "br": {
        "name": "Brazil",
        "language": "pt",
        "lang_name": "Portuguese",
        "gnews_ceid": "BR:pt-419",
        "gnews_hl": "pt-BR",
        "native_sources": [
            {"name": "Agencia Brasil", "rss": "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"},
            {"name": "Folha",          "rss": "https://feeds.folha.uol.com.br/mundo/rss091.xml"},
        ],
    },
    "kr": {
        "name": "South Korea",
        "language": "ko",
        "lang_name": "Korean",
        "gnews_ceid": "KR:ko",
        "gnews_hl": "ko",
        "native_sources": [
            {"name": "Korea Herald", "rss": "https://www.koreaherald.com/rss/020100000000.xml"},
            {"name": "Yonhap",       "rss": "https://en.yna.co.kr/RSS/news.xml"},
            {"name": "KBS World",    "rss": "https://world.kbs.co.kr/rss/rss_news.htm?lang=e"},
        ],
    },
    "sg": {
        "name": "Singapore",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "SG:en",
        "gnews_hl": "en",
        "native_sources": [
            {"name": "CNA",           "rss": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416"},
            {"name": "Straits Times", "rss": "https://www.straitstimes.com/news/world/rss.xml"},
        ],
    },
    "mx": {
        "name": "Mexico",
        "language": "es",
        "lang_name": "Spanish",
        "gnews_ceid": "MX:es-419",
        "gnews_hl": "es-419",
        "native_sources": [
            {"name": "El Universal", "rss": "https://www.eluniversal.com.mx/rss.xml"},
            {"name": "Milenio",      "rss": "https://www.milenio.com/rss"},
        ],
    },
    "za": {
        "name": "South Africa",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "ZA:en",
        "gnews_hl": "en",
        "native_sources": [
            {"name": "Daily Maverick", "rss": "https://www.dailymaverick.co.za/feed/"},
            {"name": "Mail & Guardian", "rss": "https://mg.co.za/feed/"},
        ],
    },
    "il": {
        "name": "Israel",
        "language": "en",
        "lang_name": "English",
        "gnews_ceid": "IL:en",
        "gnews_hl": "en",
        "native_sources": [
            {"name": "Haaretz",       "rss": "https://www.haaretz.com/cmlink/1.628744"},
            {"name": "Jerusalem Post","rss": "https://www.jpost.com/rss/rssfeedsworld.aspx"},
            {"name": "Times of Israel","rss": "https://www.timesofisrael.com/feed/"},
        ],
    },
    "tr": {
        "name": "Turkey",
        "language": "tr",
        "lang_name": "Turkish",
        "gnews_ceid": "TR:tr",
        "gnews_hl": "tr",
        "native_sources": [
            {"name": "TRT World",    "rss": "https://www.trtworld.com/rss"},
            {"name": "Hurriyet Daily","rss": "https://www.hurriyetdailynews.com/rss"},
            {"name": "Daily Sabah",  "rss": "https://www.dailysabah.com/rss"},
        ],
    },
    "ir": {
        "name": "Iran",
        "language": "fa",
        "lang_name": "Persian",
        "gnews_ceid": "IR:fa",
        "gnews_hl": "fa",
        "native_sources": [
            {"name": "PressTV",        "rss": "https://www.presstv.ir/rss"},
            {"name": "Tehran Times",   "rss": "https://www.tehrantimes.com/rss"},
            {"name": "Iran International", "rss": "https://www.iranintl.com/en/rss"},
            {"name": "IRNA English",   "rss": "https://en.irna.ir/rss"},
        ],
    },
}

DEFAULT_COUNTRIES = ["us", "uk", "cn", "jp", "de", "fr", "au", "in"]

GNEWS_RSS = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={cc}&ceid={ceid}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Query translation ─────────────────────────────────────────────────────────

def _translate_query(query: str, lang_name: str) -> str:
    """Translate the search query into the target language using the LLM."""
    if lang_name == "English":
        return query
    try:
        from summarizer import llm_complete
        prompt = (
            f"Translate this news search query to {lang_name}. "
            f"Return ONLY the translation, no explanation:\n{query}"
        )
        result = llm_complete(prompt, max_tokens=80)
        translated = result.strip()
        if translated:
            print(f"    [search] Query in {lang_name}: {translated}")
            return translated
    except Exception:
        pass
    return query


# ── RSS fetchers ──────────────────────────────────────────────────────────────

def _parse_rss_bytes(data: bytes) -> ET.Element:
    """
    Parse RSS/Atom XML bytes robustly.
    Falls back to stripping undeclared namespace prefixes if ET chokes.
    """
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    # Fallback: strip all xmlns declarations and namespace prefixes so ET can parse
    import re as _re
    text = data.decode("utf-8", errors="replace")
    # Remove namespace declarations
    text = _re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', text)
    # Remove namespace prefixes from tags: <ns:tag> → <tag>, </ns:tag> → </tag>
    text = _re.sub(r'<(/?)[\w][\w.-]*:([\w][\w.-]*)', r'<\1\2', text)
    return ET.fromstring(text.encode("utf-8"))


def _fetch_rss(url: str, timeout: int = 10) -> list[dict]:
    """Fetch any RSS feed and return list of {title, url, source, published}."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        # Use bytes to avoid encoding declaration conflicts
        root = _parse_rss_bytes(resp.content)
        items = []

        # RSS 2.0
        for item in root.findall(".//item"):
            title  = (item.findtext("title") or "").strip()
            link   = (item.findtext("link")  or "").strip()
            pub    = (item.findtext("pubDate") or "").strip()
            src_el = item.find("source")
            source = src_el.text.strip() if src_el is not None and src_el.text else ""
            if title and link:
                items.append({"title": title, "url": link,
                               "source": source, "published": pub})

        # Atom (with and without namespace)
        for ns_prefix in ("http://www.w3.org/2005/Atom", ""):
            ns = f"{{{ns_prefix}}}" if ns_prefix else ""
            for entry in root.findall(f".//{ns}entry"):
                title = (entry.findtext(f"{ns}title") or "").strip()
                link_el = entry.find(f"{ns}link")
                link = (link_el.get("href", "") if link_el is not None else "").strip()
                pub  = (entry.findtext(f"{ns}updated") or
                        entry.findtext(f"{ns}published") or "").strip()
                if title and link:
                    items.append({"title": title, "url": link,
                                   "source": "", "published": pub})

        return items
    except Exception as e:
        print(f"    [search] RSS failed ({url[:60]}): {e}")
        return []


def _search_gnews(query: str, cc: str, hl: str, ceid: str) -> list[dict]:
    """Search Google News RSS with country/language settings."""
    url = GNEWS_RSS.format(
        query=requests.utils.quote(query, safe=""),
        hl=hl,
        cc=cc.upper(),
        ceid=requests.utils.quote(ceid, safe=""),  # colon in "US:en" must be encoded
    )
    return _fetch_rss(url)


# ── Article scraper ───────────────────────────────────────────────────────────

def _scrape_requests(url: str, timeout: int = 12) -> dict:
    """Fast path: requests + trafilatura."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        resolved_url = resp.url
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
        return {"text": text, "title": title, "html": html,
                "url": url, "resolved_url": resolved_url}
    except Exception as e:
        return {"text": "", "title": "", "html": "", "url": url, "error": str(e)}


def _scrape_playwright(url: str, timeout: int = 20) -> dict:
    """Playwright fallback for JS-heavy pages."""
    import asyncio

    async def _run(url: str, timeout_ms: int) -> dict:
        from playwright.async_api import async_playwright
        ARGS = [
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-extensions", "--mute-audio",
        ]
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, args=ARGS)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=HEADERS["User-Agent"],
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                await page.route("**/*", lambda r: r.abort()
                    if any(b in r.request.url for b in [
                        "doubleclick", "googlesyndication", "googletagmanager",
                        "google-analytics", "facebook.com/tr", "connect.facebook",
                        "ads.", "adservice", "analytics.", "tracking.",
                    ]) else r.continue_())

                try:
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=timeout_ms * 1000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)

                # Dismiss overlays
                for sel in [
                    "button:has-text('Accept all')", "button:has-text('Accept All')",
                    "button:has-text('Accept cookies')", "button:has-text('I agree')",
                    "button:has-text('Agree')", "button:has-text('Got it')",
                    "button:has-text('Close')", "button:has-text('No thanks')",
                    "#onetrust-accept-btn-handler", ".cc-accept",
                ]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=300):
                            await btn.click(timeout=300)
                            await page.wait_for_timeout(300)
                            break
                    except Exception:
                        pass

                await page.wait_for_timeout(1000)
                resolved_url = page.url
                html = await page.content()

                text_dom = await page.evaluate("""() => {
                    const sels = ['article','[role="main"]','main',
                        '.article-body','.article__body','.story-body',
                        '.post-content','.entry-content','.content-body',
                        '#article-body','#story-body','#main-content'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.innerText && el.innerText.trim().length > 200)
                            return el.innerText.trim();
                    }
                    const clone = document.body.cloneNode(true);
                    for (const t of ['nav','footer','aside','script','style','header',
                                     '.ad','[class*="cookie"]','[class*="banner"]'])
                        clone.querySelectorAll(t).forEach(n => n.remove());
                    return clone.innerText.trim().slice(0, 8000);
                }""")

                text_tf = trafilatura.extract(html, include_comments=False,
                                              include_tables=False) or ""
                text = text_tf if len(text_tf) > len(text_dom) else text_dom

                title = await page.title()
                try:
                    og = await page.locator('meta[property="og:title"]').get_attribute(
                        "content", timeout=500)
                    if og and og.strip():
                        title = og.strip()
                except Exception:
                    pass

                await context.close()
                await browser.close()
                return {"text": text, "title": title, "html": html,
                        "url": url, "resolved_url": resolved_url}
        except Exception as e:
            return {"text": "", "title": "", "html": "", "url": url, "error": str(e)}

    try:
        return asyncio.run(_run(url, timeout))
    except Exception as e:
        return {"text": "", "title": "", "html": "", "url": url, "error": str(e)}


def _scrape_article(url: str, timeout: int = 12) -> dict:
    """Try requests first, fall back to Playwright."""
    result = _scrape_requests(url, timeout)
    if len(result.get("text", "")) >= 200:
        return result
    print(f"    (requests got {len(result.get('text',''))} chars — trying Playwright...)")
    pw = _scrape_playwright(url, timeout=max(timeout, 20))
    if len(pw.get("text", "")) > len(result.get("text", "")):
        result.update({k: pw[k] for k in ("text", "title", "html", "resolved_url")
                       if pw.get(k)})
    return result


# ── Deduplication ─────────────────────────────────────────────────────────────

def _title_fingerprint(title: str) -> str:
    """Normalized title for deduplication."""
    return re.sub(r'\W+', ' ', title.lower()).strip()


# ── Public search ─────────────────────────────────────────────────────────────

def search_topic(
    query: str,
    countries: list[str] = None,
    max_per_country: int = 3,
    scrape_full_text: bool = True,
    job_dir: Path = None,
) -> list[dict]:
    """
    Search for query across multiple countries via:
      - Google News RSS (native language)
      - Country-specific native news RSS feeds

    Returns list of article dicts:
      {country, country_name, language, lang_name, title, url,
       resolved_url, source, text, html}
    """
    if countries is None:
        countries = DEFAULT_COUNTRIES

    results = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for cc in countries:
        cfg = COUNTRIES.get(cc.lower())
        if not cfg:
            print(f"  [search] Unknown country code: {cc}")
            continue

        lang_name = cfg["lang_name"]
        print(f"  [search] {cfg['name']} [{lang_name}]: searching '{query}'...")

        # Translate query to native language for native sources
        native_query = _translate_query(query, lang_name)

        # ── Collect candidate items from all sources ──────────────────────────
        candidates: list[dict] = []

        # 1. Google News RSS (native language settings)
        gnews_items = _search_gnews(
            native_query, cc, cfg["gnews_hl"], cfg["gnews_ceid"]
        )
        for item in gnews_items:
            item["_src_type"] = "gnews"
        candidates.extend(gnews_items)

        # 2. Native RSS sources
        for src in cfg.get("native_sources", []):
            src_items = _fetch_rss(src["rss"])
            # Filter: only keep items where title matches the query (keyword overlap)
            query_words = set(re.sub(r'\W+', ' ', query.lower()).split())
            native_words = set(re.sub(r'\W+', ' ', native_query.lower()).split())
            matched = []
            for item in src_items:
                title_words = set(re.sub(r'\W+', ' ', item["title"].lower()).split())
                if query_words & title_words or native_words & title_words:
                    item["source"] = item.get("source") or src["name"]
                    item["_src_type"] = "native"
                    matched.append(item)
            if matched:
                print(f"    [{src['name']}] {len(matched)} matching items")
            candidates.extend(matched)

        # ── Scrape up to max_per_country good articles ────────────────────────
        count = 0
        for item in candidates:
            if count >= max_per_country:
                break

            url = item.get("url", "")
            if not url or not url.startswith("http"):
                continue

            # Deduplicate by URL and title
            fp = _title_fingerprint(item.get("title", ""))
            if url in seen_urls or fp in seen_titles:
                continue

            # Resolve Google News redirect URLs via HEAD
            if "news.google.com" in url:
                try:
                    r = requests.head(url, headers=HEADERS,
                                      allow_redirects=True, timeout=8)
                    resolved = r.url
                    if "google.com" not in resolved:
                        url = resolved
                except Exception:
                    pass  # keep original, Playwright will follow it

            if url in seen_urls:
                continue
            seen_urls.add(url)
            seen_titles.add(fp)

            article = {
                "country":      cc,
                "country_name": cfg["name"],
                "language":     cfg["language"],
                "lang_name":    lang_name,
                "title":        item["title"],
                "url":          url,
                "resolved_url": url,
                "source":       item.get("source", ""),
                "published":    item.get("published", ""),
                "text":         "",
                "html":         "",
            }

            if scrape_full_text:
                print(f"    Scraping [{item.get('_src_type','?')}]: {item['title'][:55]}...")
                scraped = _scrape_article(url)
                article["text"] = scraped.get("text", "")
                article["html"] = scraped.get("html", "")

                # Use final resolved URL if it's not a Google/redirect domain
                resolved = scraped.get("resolved_url", "")
                if resolved and "google.com" not in resolved and resolved != url:
                    article["url"] = resolved
                    article["resolved_url"] = resolved

                if not article["text"] or len(article["text"]) < 100:
                    print(f"    (skipped — no text)")
                    seen_urls.discard(url)
                    seen_titles.discard(fp)
                    continue

                time.sleep(0.3)

            results.append(article)
            count += 1

        print(f"    → {count} article(s) from {cfg['name']}")

    # ── Save for debugging / checkpoint ──────────────────────────────────────
    if job_dir:
        out = Path(job_dir) / "search_results.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    total_countries = len({a["country"] for a in results})
    total_languages = len({a["language"] for a in results})
    print(f"  [search] Total: {len(results)} articles from "
          f"{total_countries} countries / {total_languages} language(s)")
    return results
