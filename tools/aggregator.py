"""
aggregator.py — Multi-country article synthesis into newscast script.

Takes a list of article dicts (from news_search.py) and calls the LLM to produce
the locked JSON schema with narration_segments and chart_data.
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from summarizer import llm_complete


# ── Locked output schema ──────────────────────────────────────────────────────

SCHEMA_EXAMPLE = {
    "headline": "Global AI Race: Perspectives From 8 Nations",
    "narration_segments": [
        {"type": "overview",     "text": "...", "visual": "overview_infographic"},
        {"type": "source_scroll","country": "US", "url": "https://...", "text": "..."},
        {"type": "source_scroll","country": "UK", "url": "https://...", "text": "..."},
        {"type": "comparison",   "text": "...", "visual": "comparison_chart"},
        {"type": "timeline",     "text": "...", "visual": "timeline_infographic"},
        {"type": "closing",      "text": "...", "visual": "overview_infographic"},
    ],
    "chart_data": {
        "country_coverage": [
            {"country": "US", "articles": 2, "sentiment": "neutral"}
        ],
        "timeline_events": [
            {"year": "2020", "event": "..."}
        ],
        "comparison_table": [
            {"aspect": "Government stance", "us": "...", "uk": "...", "cn": "..."}
        ]
    },
    "key_facts": ["...", "..."],
    "lower_third_title": "AI NEWS: GLOBAL PERSPECTIVES",
    "lower_third_name": "NewscastAI Global Desk",
}


# ── Build LLM prompt ──────────────────────────────────────────────────────────

def _build_prompt(category: str, articles: list[dict], target_duration: int) -> str:
    # Format articles for the prompt
    article_blocks = []
    for i, a in enumerate(articles):
        country = a.get("country_name", a.get("country", "Unknown"))
        title = a.get("title", "")
        source = a.get("source", "")
        url = a.get("url", "")
        text = (a.get("text") or "")[:800]  # truncate to save tokens
        article_blocks.append(
            f"[Article {i+1} — {country} / {source}]\n"
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"Text: {text}\n"
        )

    articles_text = "\n---\n".join(article_blocks)

    # Figure out which countries appear in the articles
    countries_in_articles = list({a.get("country", "").upper() for a in articles if a.get("country")})

    # target word count: ~2.5 words/sec for news narration
    target_words = int(target_duration * 2.5)
    words_per_segment = max(40, target_words // max(len(articles) + 3, 6))

    prompt = f"""You are a global news editor producing a broadcast-style newscast.

Topic: {category}

You have {len(articles)} articles from these countries: {", ".join(countries_in_articles)}.

---
{articles_text}
---

Produce a JSON object (no markdown, no code fences, raw JSON only) that follows this EXACT schema:

{{
  "headline": "Short punchy broadcast headline (max 12 words)",
  "narration_segments": [
    {{
      "type": "overview",
      "text": "Opening narration: introduce the topic globally, ~{words_per_segment} words. Cite key facts.",
      "visual": "overview_infographic"
    }},
    // For EACH article, one source_scroll segment:
    {{
      "type": "source_scroll",
      "country": "COUNTRY_CODE_UPPERCASE",
      "url": "THE_ARTICLE_URL",
      "text": "~{words_per_segment} words narrating this country's angle, perspective, or reaction."
    }},
    // One comparison segment:
    {{
      "type": "comparison",
      "text": "~{words_per_segment} words comparing the different national perspectives. Note agreements and disagreements.",
      "visual": "comparison_chart"
    }},
    // One timeline/background segment:
    {{
      "type": "timeline",
      "text": "~{words_per_segment} words of historical context and background. Key dates and developments.",
      "visual": "timeline_infographic"
    }},
    // Closing segment:
    {{
      "type": "closing",
      "text": "~30 words closing statement. What to watch for. Reporting for NewscastAI Global Desk.",
      "visual": "overview_infographic"
    }}
  ],
  "chart_data": {{
    "country_coverage": [
      // One entry per country in the articles
      {{"country": "US", "articles": 2, "sentiment": "positive|negative|neutral|mixed"}}
    ],
    "timeline_events": [
      // 4-6 key historical milestones related to this topic
      {{"year": "YYYY", "event": "Short description"}}
    ],
    "comparison_table": [
      // 3-5 aspects comparing countries
      {{"aspect": "Government position", "us": "...", "uk": "...", "cn": "..."}}
    ]
  }},
  "key_facts": ["Fact 1", "Fact 2", "Fact 3"],
  "lower_third_title": "{category.upper()} — GLOBAL COVERAGE",
  "lower_third_name": "NewscastAI Global Desk"
}}

Rules:
- Raw JSON only. No markdown. No code fences. No explanation.
- narration text must be plain spoken English, no markdown formatting.
- Each narration_segments[].text must be complete sentences suitable for text-to-speech.
- Include one source_scroll segment per article provided above.
- Fill comparison_table with the country codes that actually appear in the articles.
- timeline_events: real, verifiable dates. If unsure, use approximate years.
- sentiment: your assessment of each country's media coverage tone.
"""
    return prompt


# ── Parse LLM response ────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Extract JSON from LLM response, handling various formatting issues."""
    text = text.strip()

    # Strip code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response
    match = re.search(r"\{[\s\S]+\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Strip trailing commas (common LLM mistake)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse LLM JSON response: {e}\nResponse (first 500 chars): {text[:500]}")


def _validate_and_fix(data: dict, articles: list[dict]) -> dict:
    """Ensure the schema is complete, filling defaults for missing fields."""
    # Ensure required top-level keys
    data.setdefault("headline", "Global News Update")
    data.setdefault("narration_segments", [])
    data.setdefault("chart_data", {})
    data.setdefault("key_facts", [])
    data.setdefault("lower_third_title", "GLOBAL NEWS")
    data.setdefault("lower_third_name", "NewscastAI Global Desk")

    chart = data["chart_data"]
    chart.setdefault("country_coverage", [])
    chart.setdefault("timeline_events", [])
    chart.setdefault("comparison_table", [])

    # Ensure each narration segment has required fields
    segments = data["narration_segments"]
    fixed_segments = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "overview")
        seg.setdefault("type", "overview")
        seg.setdefault("text", "")
        if seg_type == "source_scroll":
            seg.setdefault("country", "US")
            seg.setdefault("url", "")
        else:
            seg.setdefault("visual", "overview_infographic")
        fixed_segments.append(seg)
    data["narration_segments"] = fixed_segments

    # If no source_scroll segments at all, add them from articles
    scroll_segs = [s for s in fixed_segments if s["type"] == "source_scroll"]
    if not scroll_segs:
        for a in articles[:6]:
            fixed_segments.insert(1, {
                "type": "source_scroll",
                "country": a.get("country", "??").upper(),
                "url": a.get("url", ""),
                "text": (a.get("text") or a.get("title") or "")[:200],
            })

    # If country_coverage is empty, build from articles
    if not chart["country_coverage"]:
        from collections import Counter
        cc = Counter(a.get("country", "??").upper() for a in articles)
        chart["country_coverage"] = [
            {"country": c, "articles": n, "sentiment": "neutral"}
            for c, n in cc.items()
        ]

    return data


# ── Main public function ──────────────────────────────────────────────────────

def aggregate_articles(
    category: str,
    articles: list[dict],
    target_duration: int = 120,
    provider: str = None,
    job_dir: Path = None,
) -> dict:
    """
    Synthesize multi-country articles into the locked newscast JSON schema.

    Args:
        category: Topic/category string (e.g. "AI news", "war", "climate")
        articles: List of article dicts from news_search.search_topic()
        target_duration: Target total narration duration in seconds
        provider: LLM provider override (None = auto-detect)
        job_dir: If set, saves aggregated_script.json here

    Returns:
        dict matching the locked JSON schema
    """
    print(f"[aggregator] Synthesizing {len(articles)} articles for category: {category}")
    print(f"[aggregator] Target duration: {target_duration}s")

    prompt = _build_prompt(category, articles, target_duration)

    system = (
        "You are a global news editor. Output ONLY raw JSON. "
        "No markdown, no code fences, no explanation. Just the JSON object."
    )

    print(f"[aggregator] Calling LLM ({provider or 'auto'})...")
    raw = llm_complete(prompt, system=system, max_tokens=4000, provider=provider)

    if not raw or not raw.strip():
        raise RuntimeError("LLM returned empty response")

    print(f"[aggregator] LLM response length: {len(raw)} chars")

    try:
        data = _extract_json(raw)
    except ValueError as e:
        print(f"[aggregator] JSON parse failed: {e}")
        print(f"[aggregator] Building fallback script from articles...")
        data = _build_fallback(category, articles, target_duration)

    data = _validate_and_fix(data, articles)

    # Save to job_dir
    if job_dir:
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        out_path = Path(job_dir) / "aggregated_script.json"
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[aggregator] Saved to {out_path}")

    print(f"[aggregator] Headline: {data.get('headline', '')}")
    print(f"[aggregator] Segments: {len(data.get('narration_segments', []))}")
    return data


def _build_fallback(category: str, articles: list[dict], target_duration: int) -> dict:
    """Build a minimal valid script from articles when LLM fails."""
    from collections import Counter

    segments = []
    total_words = int(target_duration * 2.5)
    words_each = max(40, total_words // (len(articles) + 3))

    # Overview
    segments.append({
        "type": "overview",
        "text": f"Today we cover {category} from a global perspective, with reports from {len(articles)} countries.",
        "visual": "overview_infographic",
    })

    # Source scrolls
    for a in articles[:8]:
        text = (a.get("text") or a.get("title") or "")
        # Take first ~words_each words
        words = text.split()[:words_each]
        narr = " ".join(words)
        if not narr:
            narr = a.get("title", "No content available.")
        segments.append({
            "type": "source_scroll",
            "country": a.get("country", "??").upper(),
            "url": a.get("url", ""),
            "text": narr,
        })

    # Comparison
    countries = list({a.get("country_name", a.get("country", "Unknown")) for a in articles})
    segments.append({
        "type": "comparison",
        "text": f"Coverage of {category} varies across nations. "
                f"Our survey spans {', '.join(countries[:5])}. "
                "Each country brings its own perspective to this global story.",
        "visual": "comparison_chart",
    })

    # Timeline
    segments.append({
        "type": "timeline",
        "text": f"The story of {category} has developed over recent years, "
                "shaped by technological, political, and social forces across the globe.",
        "visual": "timeline_infographic",
    })

    # Closing
    segments.append({
        "type": "closing",
        "text": f"That's our global report on {category}. Stay tuned for more coverage. Reporting for NewscastAI Global Desk.",
        "visual": "overview_infographic",
    })

    cc = Counter(a.get("country", "??").upper() for a in articles)

    return {
        "headline": f"{category.title()} — Global Coverage",
        "narration_segments": segments,
        "chart_data": {
            "country_coverage": [
                {"country": c, "articles": n, "sentiment": "neutral"}
                for c, n in cc.items()
            ],
            "timeline_events": [
                {"year": "2020", "event": f"Early developments in {category}"},
                {"year": "2022", "event": "Acceleration of global coverage"},
                {"year": "2024", "event": "Major policy responses"},
                {"year": "2025", "event": "Current situation"},
            ],
            "comparison_table": [
                {"aspect": "Media coverage", **{a.get("country", "??"): "Active" for a in articles[:5]}},
            ],
        },
        "key_facts": [
            f"{len(articles)} articles collected from {len(cc)} countries",
            f"Topic: {category}",
        ],
        "lower_third_title": f"{category.upper()} — GLOBAL COVERAGE",
        "lower_third_name": "NewscastAI Global Desk",
    }


if __name__ == "__main__":
    # Quick test
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from news_search import search_topic

    category = sys.argv[1] if len(sys.argv) > 1 else "artificial intelligence"
    print(f"Searching for: {category}")
    articles = search_topic(category, max_per_country=1, scrape_full_text=False)
    print(f"Found {len(articles)} articles")

    result = aggregate_articles(category, articles, target_duration=90)
    print(json.dumps(result, indent=2)[:2000])
