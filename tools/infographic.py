"""
infographic.py — Generate self-contained HTML infographic pages.

Produces three types of pages from aggregated script data:
  1. overview_infographic  — headline card + key facts + country badge grid
  2. comparison_chart      — Chart.js horizontal bar chart (country coverage + comparison table)
  3. timeline_infographic  — CSS vertical timeline of key events

All pages are self-contained (CDN for Chart.js, inline CSS/JS).
Playwright records each page for the final video.
"""

import json
from pathlib import Path
from typing import Optional


# ── Colour palette ────────────────────────────────────────────────────────────

PALETTE = {
    "bg":      "#0a0e1a",
    "card":    "#141927",
    "accent":  "#1a73e8",
    "accent2": "#ea4335",
    "accent3": "#fbbc05",
    "text":    "#e8eaf6",
    "muted":   "#8892b0",
    "green":   "#34a853",
    "border":  "#1e2a40",
}

SENTIMENT_COLORS = {
    "positive": "#34a853",
    "negative": "#ea4335",
    "neutral":  "#1a73e8",
    "mixed":    "#fbbc05",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _css_vars() -> str:
    return "\n".join(f"  --{k}: {v};" for k, v in PALETTE.items())


def _base_style() -> str:
    return f"""
<style>
  :root {{
{_css_vars()}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 40px 60px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 28px 36px;
  }}
  h1 {{ font-size: 2.4rem; font-weight: 700; line-height: 1.2; }}
  h2 {{ font-size: 1.6rem; font-weight: 600; margin-bottom: 16px; }}
  h3 {{ font-size: 1.1rem; font-weight: 600; color: var(--muted); text-transform: uppercase;
        letter-spacing: 0.08em; margin-bottom: 12px; }}
  .accent {{ color: var(--accent); }}
  .logo {{
    font-size: 0.85rem; font-weight: 700; letter-spacing: 0.15em;
    text-transform: uppercase; color: var(--accent); margin-bottom: 20px;
    display: flex; align-items: center; gap: 8px;
  }}
  .logo::before {{
    content: '';
    display: inline-block;
    width: 8px; height: 8px;
    background: var(--accent2);
    border-radius: 50%;
    animation: blink 1s infinite;
  }}
  @keyframes blink {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:0 }} }}
</style>
"""


# ── 1. Overview infographic ───────────────────────────────────────────────────

def build_overview(script: dict) -> str:
    headline = script.get("headline", "Global News Coverage")
    key_facts = script.get("key_facts", [])
    lower_title = script.get("lower_third_title", "GLOBAL NEWS")
    lower_name = script.get("lower_third_name", "NewscastAI Global Desk")

    # Country badges from chart_data
    coverage = script.get("chart_data", {}).get("country_coverage", [])

    facts_html = ""
    for fact in key_facts[:6]:
        facts_html += f'<li class="fact-item">{fact}</li>\n'

    badge_html = ""
    for c in coverage[:12]:
        sentiment = c.get("sentiment", "neutral")
        color = SENTIMENT_COLORS.get(sentiment, PALETTE["accent"])
        badge_html += f"""
        <div class="badge" style="border-color:{color}">
          <span class="badge-cc">{c['country']}</span>
          <span class="badge-n">{c['articles']} art.</span>
          <span class="badge-s" style="color:{color}">{sentiment}</span>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{headline}</title>
{_base_style()}
<style>
  .container {{ width: 100%; max-width: 1400px; }}
  .headline-card {{ margin-bottom: 32px; }}
  .headline-card h1 {{ font-size: 3rem; margin-bottom: 8px; }}
  .sub-label {{ font-size: 1rem; color: var(--muted); margin-bottom: 24px; }}
  .facts-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
    gap: 14px; margin-bottom: 32px;
  }}
  .fact-item {{
    list-style: none;
    background: var(--card); border: 1px solid var(--border);
    border-left: 4px solid var(--accent);
    border-radius: 8px; padding: 14px 18px;
    font-size: 1.05rem; line-height: 1.5;
  }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .badge {{
    border: 1px solid; border-radius: 8px;
    padding: 10px 16px; display: flex; flex-direction: column; gap: 3px;
    min-width: 90px; align-items: center;
  }}
  .badge-cc {{ font-weight: 700; font-size: 1.1rem; }}
  .badge-n  {{ font-size: 0.8rem; color: var(--muted); }}
  .badge-s  {{ font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }}
  .footer-bar {{
    margin-top: 40px; padding-top: 16px;
    border-top: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
  }}
  .footer-title {{ font-size: 1.1rem; font-weight: 700; color: var(--accent3); }}
  .footer-name  {{ font-size: 0.9rem; color: var(--muted); }}
</style>
</head>
<body>
<div class="container">
  <div class="logo">&#9679; NEWSCAST AI — LIVE</div>
  <div class="card headline-card">
    <h1>{headline}</h1>
    <p class="sub-label">{lower_title}</p>
  </div>

  <h3>Key Facts</h3>
  <ul class="facts-grid">
{facts_html}  </ul>

  <h3>Coverage by Country</h3>
  <div class="badges">{badge_html}</div>

  <div class="footer-bar">
    <span class="footer-title">{lower_title}</span>
    <span class="footer-name">{lower_name}</span>
  </div>
</div>
</body>
</html>
"""


# ── 2. Comparison chart ───────────────────────────────────────────────────────

def build_comparison(script: dict) -> str:
    headline = script.get("headline", "Global Comparison")
    chart_data = script.get("chart_data", {})
    coverage = chart_data.get("country_coverage", [])
    comparison_table = chart_data.get("comparison_table", [])

    # Chart.js data
    labels = json.dumps([c["country"] for c in coverage])
    counts = json.dumps([c["articles"] for c in coverage])
    colors = json.dumps([SENTIMENT_COLORS.get(c.get("sentiment", "neutral"), PALETTE["accent"]) for c in coverage])

    # Comparison table HTML
    table_html = ""
    if comparison_table:
        # Get all column keys except "aspect"
        cols = list({k for row in comparison_table for k in row if k != "aspect"})
        cols_sorted = sorted(cols)

        header_cells = "".join(f"<th>{c.upper()}</th>" for c in cols_sorted)
        table_html = f"""
    <h3 style="margin-top:40px">Perspective Comparison</h3>
    <table>
      <thead><tr><th>Aspect</th>{header_cells}</tr></thead>
      <tbody>"""
        for row in comparison_table:
            cells = "".join(f"<td>{row.get(c, '—')}</td>" for c in cols_sorted)
            table_html += f"<tr><td class='aspect'>{row.get('aspect','')}</td>{cells}</tr>\n"
        table_html += "</tbody></table>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{headline} — Comparison</title>
{_base_style()}
<style>
  .container {{ width: 100%; max-width: 1400px; }}
  .chart-wrap {{ width: 100%; max-width: 900px; margin: 0 auto 40px; }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 0.95rem;
    margin-top: 12px;
  }}
  th, td {{
    padding: 11px 16px; text-align: left;
    border-bottom: 1px solid var(--border);
  }}
  th {{ background: var(--card); color: var(--accent); font-weight: 600; font-size: 0.85rem;
        text-transform: uppercase; letter-spacing: 0.05em; }}
  tr:hover td {{ background: rgba(26,115,232,0.06); }}
  td.aspect {{ font-weight: 600; color: var(--text); }}
  td {{ color: var(--muted); }}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
</head>
<body>
<div class="container">
  <div class="logo">&#9679; NEWSCAST AI — ANALYSIS</div>
  <h2><span class="accent">Coverage</span> by Country</h2>
  <div class="chart-wrap">
    <canvas id="coverageChart" height="320"></canvas>
  </div>
  {table_html}
</div>
<script>
  Chart.defaults.color = '#8892b0';
  Chart.defaults.borderColor = '#1e2a40';
  new Chart(document.getElementById('coverageChart'), {{
    type: 'bar',
    data: {{
      labels: {labels},
      datasets: [{{
        label: 'Articles collected',
        data: {counts},
        backgroundColor: {colors},
        borderRadius: 6,
        borderSkipped: false,
      }}]
    }},
    options: {{
      indexAxis: 'y',
      animation: {{ duration: 1200, easing: 'easeInOutQuart' }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{
          label: ctx => ` ${{ctx.parsed.x}} article(s)`
        }} }}
      }},
      scales: {{
        x: {{ grid: {{ color: '#1e2a40' }}, ticks: {{ stepSize: 1 }} }},
        y: {{ grid: {{ display: false }} }},
      }}
    }}
  }});
</script>
</body>
</html>
"""


# ── 3. Timeline infographic ───────────────────────────────────────────────────

def build_timeline(script: dict) -> str:
    headline = script.get("headline", "Timeline")
    events = script.get("chart_data", {}).get("timeline_events", [])

    items_html = ""
    for i, ev in enumerate(events):
        side = "left" if i % 2 == 0 else "right"
        items_html += f"""
    <div class="tl-item tl-{side}">
      <div class="tl-dot"></div>
      <div class="tl-card card">
        <div class="tl-year">{ev.get('year','')}</div>
        <div class="tl-event">{ev.get('event','')}</div>
      </div>
    </div>"""

    if not items_html:
        items_html = '<div class="tl-item tl-left"><div class="tl-dot"></div><div class="tl-card card"><div class="tl-year">2024</div><div class="tl-event">Global coverage begins</div></div></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{headline} — Timeline</title>
{_base_style()}
<style>
  body {{ justify-content: flex-start; padding-top: 60px; }}
  .container {{ width: 100%; max-width: 1200px; }}
  h2 {{ text-align: center; margin-bottom: 48px; }}
  .timeline {{
    position: relative;
    padding: 0 20px;
  }}
  .timeline::before {{
    content: '';
    position: absolute; left: 50%; top: 0; bottom: 0;
    width: 3px; background: var(--border); transform: translateX(-50%);
  }}
  .tl-item {{
    display: flex; margin-bottom: 40px; position: relative;
    animation: fadeSlide 0.6s ease both;
  }}
  .tl-left  {{ justify-content: flex-start;  padding-right: calc(50% + 36px); }}
  .tl-right {{ justify-content: flex-end;    padding-left:  calc(50% + 36px); }}
  .tl-dot {{
    position: absolute; left: calc(50% - 10px); top: 14px;
    width: 20px; height: 20px; border-radius: 50%;
    background: var(--accent); border: 3px solid var(--bg);
    z-index: 2;
  }}
  .tl-card {{ width: 100%; }}
  .tl-year {{
    font-size: 1.4rem; font-weight: 700; color: var(--accent);
    margin-bottom: 6px;
  }}
  .tl-event {{ font-size: 1rem; line-height: 1.6; color: var(--text); }}
  @keyframes fadeSlide {{
    from {{ opacity: 0; transform: translateY(20px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .tl-item:nth-child(1)  {{ animation-delay: 0.1s }}
  .tl-item:nth-child(2)  {{ animation-delay: 0.3s }}
  .tl-item:nth-child(3)  {{ animation-delay: 0.5s }}
  .tl-item:nth-child(4)  {{ animation-delay: 0.7s }}
  .tl-item:nth-child(5)  {{ animation-delay: 0.9s }}
  .tl-item:nth-child(6)  {{ animation-delay: 1.1s }}
</style>
</head>
<body>
<div class="container">
  <div class="logo" style="justify-content:center">&#9679; NEWSCAST AI — HISTORY & CONTEXT</div>
  <h2><span class="accent">{headline}</span> — Timeline</h2>
  <div class="timeline">
{items_html}
  </div>
</div>
</body>
</html>
"""


# ── Public API ────────────────────────────────────────────────────────────────

def generate_infographics(script: dict, job_dir: Path) -> dict[str, Path]:
    """
    Generate all three HTML infographic pages and save to job_dir/infographics/.

    Returns:
        dict mapping visual_name → html file path:
          "overview_infographic" → .../overview.html
          "comparison_chart"     → .../comparison.html
          "timeline_infographic" → .../timeline.html
    """
    out_dir = Path(job_dir) / "infographics"
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = {
        "overview_infographic": ("overview.html",   build_overview),
        "comparison_chart":     ("comparison.html", build_comparison),
        "timeline_infographic": ("timeline.html",   build_timeline),
    }

    result = {}
    for key, (filename, builder) in pages.items():
        path = out_dir / filename
        html = builder(script)
        path.write_text(html, encoding="utf-8")
        result[key] = path
        print(f"[infographic] Saved {key}: {path}")

    return result


if __name__ == "__main__":
    import sys, json
    # Test with sample data
    sample = {
        "headline": "AI Regulation: A Global Patchwork",
        "key_facts": [
            "EU AI Act came into force in August 2024",
            "China requires AI content labelling since 2023",
            "US relies on voluntary commitments from major labs",
        ],
        "lower_third_title": "AI REGULATION — GLOBAL COVERAGE",
        "lower_third_name": "NewscastAI Global Desk",
        "chart_data": {
            "country_coverage": [
                {"country": "US", "articles": 2, "sentiment": "mixed"},
                {"country": "UK", "articles": 2, "sentiment": "positive"},
                {"country": "CN", "articles": 2, "sentiment": "neutral"},
                {"country": "DE", "articles": 1, "sentiment": "positive"},
                {"country": "JP", "articles": 1, "sentiment": "neutral"},
                {"country": "AU", "articles": 1, "sentiment": "neutral"},
            ],
            "timeline_events": [
                {"year": "2021", "event": "EU proposes AI Act — first comprehensive AI regulation"},
                {"year": "2022", "event": "China enacts deep-fake and recommendation algorithm rules"},
                {"year": "2023", "event": "ChatGPT triggers global regulatory wave"},
                {"year": "2024", "event": "EU AI Act enters into force; US Executive Order on AI"},
                {"year": "2025", "event": "National AI strategies proliferate across G20"},
            ],
            "comparison_table": [
                {"aspect": "Regulatory approach", "us": "Voluntary", "uk": "Sector-led", "cn": "State-directed"},
                {"aspect": "AI Act equivalent",    "us": "None yet",  "uk": "Proposed",  "cn": "Patchwork laws"},
                {"aspect": "Data localisation",    "us": "Minimal",   "uk": "Moderate",  "cn": "Strict"},
            ],
        },
    }
    out = Path("/tmp/infographic_test")
    pages = generate_infographics(sample, out)
    for k, p in pages.items():
        print(f"  {k}: {p}")
