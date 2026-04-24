"""
stock_footage.py — Free stock video from Pexels, Pixabay, and Coverr APIs.

Usage:
    from stock_footage import search_stock_video
    paths = search_stock_video("climate change", job_id="xyz", seg_index=0, duration=15)

Sources tried in order:
    1. Pexels API (free key required)
    2. Pixabay API (free key required)
    3. Coverr API (free, no key required)

Each source returns short clips (3-5s each), multiple clips are downloaded
and will be concatenated by the pipeline.
"""

import json
import subprocess
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional

TEMP_DIR = Path(__file__).parent.parent / "temp"

# ── API keys (set these, or leave empty to skip that source) ─────────────────
# Get free keys at:
#   Pexels:  https://www.pexels.com/api/
#   Pixabay: https://pixabay.com/api/docs/
# Or set environment variables: PEXELS_API_KEY, PIXABAY_API_KEY
import os as _sf_os
PEXELS_API_KEY = _sf_os.environ.get("PEXELS_API_KEY", "Gw9B1WlhnyNOCG7rs7PRNxtoxeT9gDO0EkyWcZ385mn6ZQxWz9NeQs1w")
PIXABAY_API_KEY = _sf_os.environ.get("PIXABAY_API_KEY", "55477421-e1e5cda74ff4f0f850e93651c")


# ── Pexels API ───────────────────────────────────────────────────────────────

def _search_pexels(query: str, per_page: int = 10, page: int = 1) -> list[dict]:
    """Search Pexels videos API. Returns list of video info dicts."""
    if not PEXELS_API_KEY:
        return []

    url = (
        f"https://api.pexels.com/videos/search"
        f"?query={urllib.parse.quote(query)}&per_page={per_page}&size=medium&page={page}"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": PEXELS_API_KEY,
        "User-Agent": "NewscastAI/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        for v in data.get("videos", []):
            # Find best video file (mp4, ~720p)
            video_files = v.get("video_files", [])
            best = None
            for vf in video_files:
                if vf.get("link", "").endswith(".mp4"):
                    if best is None or vf.get("width", 0) > best.get("width", 0):
                        best = vf
            if best and best.get("link"):
                results.append({
                    "id": str(v.get("id", "")),
                    "title": v.get("description", "")[:80],
                    "duration": v.get("duration", 15),
                    "url": best["link"],
                    "width": best.get("width", 1280),
                    "height": best.get("height", 720),
                    "source": "pexels",
                })
        return results
    except Exception as e:
        print(f"  [pexels] search failed: {e}")
        return []


def _download_pexels(video_url: str, output_path: Path) -> bool:
    """Download a Pexels video (direct MP4 link)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            "curl", "-L", "-s", "-o", str(output_path),
            "--connect-timeout", "15", "--max-time", "120",
            video_url,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
        return output_path.exists() and output_path.stat().st_size > 10_000
    except Exception:
        # Fallback to urllib
        try:
            with urllib.request.urlopen(video_url, timeout=60) as resp:
                output_path.write_bytes(resp.read())
            return output_path.exists() and output_path.stat().st_size > 10_000
        except Exception as e:
            print(f"  [pexels] download failed: {e}")
            return False


# ── Pixabay API ──────────────────────────────────────────────────────────────

def _search_pixabay(query: str, per_page: int = 10, page: int = 1) -> list[dict]:
    """Search Pixabay videos API. Returns list of video info dicts."""
    if not PIXABAY_API_KEY:
        return []

    url = (
        f"https://pixabay.com/api/videos/"
        f"?key={PIXABAY_API_KEY}&q={urllib.parse.quote(query)}"
        f"&per_page={per_page}&video_type=all&page={page}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        for v in data.get("hits", []):
            # Pick medium quality video
            videos = v.get("videos", {})
            medium = videos.get("medium", videos.get("small", videos.get("large", {})))
            if medium and medium.get("url"):
                results.append({
                    "id": str(v.get("id", "")),
                    "title": v.get("tags", "")[:80],
                    "duration": v.get("duration", 15),
                    "url": medium["url"],
                    "width": medium.get("width", 1280),
                    "height": medium.get("height", 720),
                    "source": "pixabay",
                })
        return results
    except Exception as e:
        print(f"  [pixabay] search failed: {e}")
        return []


def _download_pixabay(video_url: str, output_path: Path) -> bool:
    """Download a Pixabay video (direct MP4 link)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            "curl", "-L", "-s", "-o", str(output_path),
            "--connect-timeout", "15", "--max-time", "120",
            video_url,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
        return output_path.exists() and output_path.stat().st_size > 10_000
    except Exception:
        try:
            with urllib.request.urlopen(video_url, timeout=60) as resp:
                output_path.write_bytes(resp.read())
            return output_path.exists() and output_path.stat().st_size > 10_000
        except Exception as e:
            print(f"  [pixabay] download failed: {e}")
            return False


# ── Coverr API ───────────────────────────────────────────────────────────────

def _search_coverr(query: str, per_page: int = 10, page: int = 1) -> list[dict]:
    """Search Coverr videos API (free, no API key required). Returns list of video info dicts.
    Note: Coverr requires a second API call per video to get download URLs."""
    url = (
        f"https://coverr.co/api/videos"
        f"?query={urllib.parse.quote(query)}&hitsPerPage={per_page}&page={page}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "NewscastAI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        for v in data.get("hits", [])[:5]:  # Limit to 5 to avoid too many detail calls
            video_id = v.get("id", "")
            # Get download URL from detail endpoint
            detail_url = f"https://coverr.co/api/videos/{video_id}"
            try:
                detail_req = urllib.request.Request(detail_url, headers={"User-Agent": "NewscastAI/1.0"})
                with urllib.request.urlopen(detail_req, timeout=10) as detail_resp:
                    detail = json.loads(detail_resp.read())
                urls = detail.get("urls", {})
                coverr_url = urls.get("mp4") or urls.get("mp4_preview", "")
            except Exception:
                coverr_url = ""

            if coverr_url:
                results.append({
                    "id": video_id,
                    "title": v.get("title", "")[:80] or v.get("description", "")[:80],
                    "duration": v.get("duration", 15),
                    "url": coverr_url,
                    "width": v.get("max_width", 1920),
                    "height": v.get("max_height", 1080),
                    "source": "coverr",
                })
        return results
    except Exception as e:
        print(f"  [coverr] search failed: {e}")
        return []


def _download_coverr(video_url: str, output_path: Path) -> bool:
    """Download a Coverr video (direct MP4 link)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            "curl", "-L", "-s", "-o", str(output_path),
            "--connect-timeout", "15", "--max-time", "120",
            video_url,
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=130)
        return output_path.exists() and output_path.stat().st_size > 10_000
    except Exception:
        try:
            with urllib.request.urlopen(video_url, timeout=60) as resp:
                output_path.write_bytes(resp.read())
            return output_path.exists() and output_path.stat().st_size > 10_000
        except Exception as e:
            print(f"  [coverr] download failed: {e}")
            return False


# ── Multi-source search & download ───────────────────────────────────────────

def search_stock_video(
    query: str,
    job_id: str,
    seg_index: int,
    duration: float = 15.0,
    clip_length: float = 4.0,
    page: int = 1,
) -> list[Path]:
    """
    Search Pexels + Pixabay + Coverr for stock footage matching query.
    Downloads multiple short clips (~clip_length seconds each) to fill duration.

    The `page` parameter allows fetching different results for the same query
    across segments - increment page to get different clips.

    Returns list of MP4 paths, or empty list if nothing found.
    """
    num_clips = max(1, int(duration / clip_length))
    broll_dir = TEMP_DIR / job_id / "broll"
    broll_dir.mkdir(parents=True, exist_ok=True)

    all_results = []
    downloaded = []

    # Try Pexels first (with page offset for variety)
    print(f"  [stock] Searching Pexels: '{query[:50]}' (page={page})")
    pexels_results = _search_pexels(query, per_page=10, page=page)
    all_results.extend(pexels_results[:num_clips])

    # Try Pixabay (with page offset)
    print(f"  [stock] Searching Pixabay: '{query[:50]}' (page={page})")
    pixabay_results = _search_pixabay(query, per_page=10, page=page)
    all_results.extend(pixabay_results[:num_clips])

    # Try Coverr (with page offset)
    print(f"  [stock] Searching Coverr: '{query[:50]}' (page={page})")
    coverr_results = _search_coverr(query, per_page=10, page=page)
    all_results.extend(coverr_results[:num_clips])

    if not all_results:
        print(f"  [stock] No results from Pexels, Pixabay, or Coverr")
        return []

    # Download clips - use unique naming per segment to avoid cache reuse
    for i, vid in enumerate(all_results[:num_clips]):
        source = vid["source"]
        vid_id = vid["id"]
        # Include seg_index in filename so each segment gets its own copy
        output_path = broll_dir / f"stock_s{seg_index:02d}_{source}_{vid_id}_p{page}.mp4"

        # Skip portrait/vertical videos (height > width) - we need landscape for newscast
        w, h = vid.get("width", 1920), vid.get("height", 1080)
        if h > w:
            print(f"  [stock] Skipping portrait clip ({source}): {vid_id} ({w}x{h})")
            continue

        if source == "pexels":
            ok = _download_pexels(vid["url"], output_path)
        elif source == "pixabay":
            ok = _download_pixabay(vid["url"], output_path)
        elif source == "coverr":
            ok = _download_coverr(vid["url"], output_path)
        else:
            ok = False

        if ok:
            print(f"  [stock] Downloaded ({source}): {output_path.name}")
            downloaded.append(output_path)
        else:
            print(f"  [stock] Failed ({source}): {vid_id}")

    if not downloaded:
        print(f"  [stock] All downloads failed")

    return downloaded


def _build_stock_query(segment: dict, headline: str = "") -> str:
    """Build a search query for stock video from a narration segment."""
    seg_type = segment.get("type", "")
    text = segment.get("text", "")
    country = segment.get("country", "")

    # Extract key terms
    words = text.split()[:10]
    snippet = " ".join(words)

    # Build topic-specific queries
    topic_hints = {
        "ai": "artificial intelligence technology",
        "climate": "climate change environment",
        "crypto": "cryptocurrency bitcoin blockchain",
        "war": "military conflict defense",
        "election": "politics voting government",
        "tech": "technology smartphone computer",
        "health": "healthcare medical hospital",
        "economy": "economy finance business",
        "regulation": "law regulation policy",
    }

    # Try to match topic from headline or text
    combined = (headline + " " + snippet).lower()
    for keyword, stock_query in topic_hints.items():
        if keyword in combined:
            return stock_query

    # Generic fallback
    if seg_type == "overview":
        return f"news broadcast journalism"
    elif seg_type == "timeline":
        return f"history timeline events"
    elif seg_type == "comparison":
        return f"data analysis comparison"
    elif seg_type == "closing":
        return f"city skyline aerial"
    elif country:
        return f"{country} news city"

    return snippet[:50] if snippet else "news world"


# ── Public API ───────────────────────────────────────────────────────────────

def find_broll_for_segment(
    segment: dict,
    job_id: str,
    seg_index: int,
    headline: str = "",
    duration: float = 15.0,
) -> list[Path]:
    """
    Search all stock video sources for clips matching this segment.
    Returns list of MP4 paths (multiple short clips to be concatenated).
    """
    query = _build_stock_query(segment, headline)
    return search_stock_video(query, job_id, seg_index, duration=duration)


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "artificial intelligence"
    print(f"Searching stock video for: '{query}'")

    # Quick test
    pexels = _search_pexels(query, per_page=3)
    print(f"  Pexels: {len(pexels)} results")
    for v in pexels:
        print(f"    [{v['source']}] {v['duration']}s {v['title'][:50]} ({v['width']}x{v['height']})")

    pixabay = _search_pixabay(query, per_page=3)
    print(f"  Pixabay: {len(pixabay)} results")
    for v in pixabay:
        print(f"    [{v['source']}] {v['duration']}s {v['title'][:50]} ({v['width']}x{v['height']})")

    coverr = _search_coverr(query, per_page=3)
    print(f"  Coverr: {len(coverr)} results")
    for v in coverr:
        print(f"    [{v['source']}] {v['duration']}s {v['title'][:50]} ({v['width']}x{v['height']})")
