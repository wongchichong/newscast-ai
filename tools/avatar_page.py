"""
avatar_page.py — Generate and record talking avatar HTML pages.

Used as fallback when:
  - No article URL available for source_scroll segment
  - Page recording fails
  - Infographic data is sparse
"""

import json
import shutil
from pathlib import Path

TEMPLATE = Path(__file__).parent / "avatar_template.html"
TEMP_DIR  = Path(__file__).parent.parent / "temp"


def make_avatar_html(
    segment: dict,
    script: dict,
    output_html: Path,
    duration: float = 30.0,
) -> Path:
    """
    Generate an avatar HTML page populated with segment data.
    Returns path to the generated HTML.
    """
    headline = script.get("headline", "Global News")
    lower_title = script.get("lower_third_title", "GLOBAL NEWS")
    lower_sub   = script.get("lower_third_name", "NewscastAI Global Desk")

    country = segment.get("country", "")
    text    = segment.get("text", "")
    url     = segment.get("url", "")

    # Source name from URL domain
    source = "NewscastAI"
    if url:
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.replace("www.", "")
            source = domain.split(".")[0].upper() if domain else source
        except Exception:
            pass

    data = {
        "headline":       headline[:80],
        "country":        country or "GLOBAL",
        "speech_text":    text,
        "source":         source,
        "lower_title":    lower_title,
        "lower_subtitle": lower_sub,
        "duration":       duration,
    }

    template_src = TEMPLATE.read_text(encoding="utf-8")
    # Inject data into the template
    injected = template_src.replace(
        "window.__AVATAR_DATA__ || {};",
        f"window.__AVATAR_DATA__ || {json.dumps(data, ensure_ascii=False)};"
    ).replace(
        "const DATA = window.__AVATAR_DATA__ || {};",
        f"const DATA = {json.dumps(data, ensure_ascii=False)};"
    )

    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(injected, encoding="utf-8")
    return output_html


def record_avatar(
    segment: dict,
    script: dict,
    output_mp4: Path,
    duration: float = 30.0,
    job_id: str = "avatar",
) -> Path | None:
    """
    Generate and record an avatar page for a segment.
    Returns path to MP4, or None on failure.
    """
    avatar_dir = TEMP_DIR / job_id / "avatar_pages"
    avatar_dir.mkdir(parents=True, exist_ok=True)

    seg_idx = segment.get("_index", 0)
    html_path = avatar_dir / f"seg_{seg_idx:02d}_avatar.html"

    make_avatar_html(segment, script, html_path, duration=duration)

    try:
        # Import here to avoid circular imports
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from playwright_scraper import record_html_page
        record_html_page(html_path, output_mp4, duration=duration)
        if output_mp4.exists() and output_mp4.stat().st_size > 10_000:
            return output_mp4
    except Exception as e:
        print(f"  [avatar] Recording failed: {e}")

    return None
