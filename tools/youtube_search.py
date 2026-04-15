"""
youtube_search.py — Search YouTube for relevant news clips and extract highlights.

For each narration segment, searches YouTube for related footage,
downloads a short clip (30–60s), and returns it as B-roll video.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

TEMP_DIR = Path(__file__).parent.parent / "temp"
YT_DLP   = "yt-dlp"


def _build_query(segment: dict, headline: str = "") -> str:
    """Build a YouTube search query from a narration segment."""
    seg_type = segment.get("type", "")
    text     = segment.get("text", "")
    country  = segment.get("country", "")

    # Extract key noun phrases from the text (first 15 words)
    words = text.split()[:15]
    snippet = " ".join(words)

    if seg_type == "source_scroll" and country:
        return f"{country} news {headline[:40]}"
    elif seg_type == "comparison":
        return f"global news comparison {headline[:40]}"
    elif seg_type == "timeline":
        return f"history timeline {headline[:40]}"
    elif headline:
        return headline[:60]
    return snippet[:60]


def _search_youtube(query: str, max_results: int = 5) -> list[dict]:
    """
    Search YouTube and return list of video info dicts.
    Uses yt-dlp's search functionality (no API key needed).
    """
    try:
        cmd = [
            YT_DLP,
            f"ytsearch{max_results}:{query}",
            "--dump-json",
            "--no-playlist",
            "--flat-playlist",
            "--no-warnings",
            "--quiet",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        results = []
        for line in r.stdout.strip().splitlines():
            try:
                data = json.loads(line)
                duration = data.get("duration") or 0
                # Prefer news clips 30s–5min long
                if 20 <= duration <= 300:
                    results.append({
                        "id":       data.get("id", ""),
                        "title":    data.get("title", ""),
                        "duration": duration,
                        "url":      data.get("url") or f"https://www.youtube.com/watch?v={data.get('id','')}",
                        "uploader": data.get("uploader", ""),
                    })
            except Exception:
                pass
        return results
    except Exception as e:
        print(f"  [youtube] search failed: {e}")
        return []


def _download_clip(video_id: str, output_path: Path,
                   start_sec: float = 0, duration_sec: float = 60) -> bool:
    """
    Download a clip from a YouTube video using yt-dlp + ffmpeg.
    Downloads only the needed segment via --download-sections.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # yt-dlp section format: *start-end
    end_sec = start_sec + duration_sec
    section = f"*{start_sec:.0f}-{end_sec:.0f}"

    cmd = [
        YT_DLP,
        url,
        "--download-sections", section,
        "--format", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--output", str(output_path),
        "--force-keyframes-at-cuts",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if output_path.exists() and output_path.stat().st_size > 50_000:
            return True
        # Try without section if that failed
        cmd_full = [
            YT_DLP, url,
            "--format", "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "--merge-output-format", "mp4",
            "--no-playlist", "--no-warnings", "--quiet",
            "--output", str(output_path),
        ]
        r2 = subprocess.run(cmd_full, capture_output=True, text=True, timeout=120)
        return output_path.exists() and output_path.stat().st_size > 50_000
    except subprocess.TimeoutExpired:
        print(f"  [youtube] download timeout for {video_id}")
        return False
    except Exception as e:
        print(f"  [youtube] download error: {e}")
        return False


def _trim_to_duration(input_mp4: Path, output_mp4: Path, duration: float) -> bool:
    """Trim or pad a video clip to exact duration."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_mp4),
        "-t", str(duration),
        "-vf", f"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-c:a", "aac",
        "-r", "30",
        str(output_mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and output_mp4.exists()


def find_broll_for_segment(
    segment: dict,
    job_id: str,
    seg_index: int,
    headline: str = "",
    duration: float = 30.0,
) -> Path | None:
    """
    Search YouTube for a relevant clip for this narration segment.
    Returns Path to a trimmed MP4 clip, or None if not found.

    Only used for source_scroll and overview segments (not infographic visuals).
    """
    query = _build_query(segment, headline)
    print(f"  [youtube] Searching for B-roll: '{query}'")

    results = _search_youtube(query, max_results=5)
    if not results:
        print(f"  [youtube] No results for: {query}")
        return None

    broll_dir = TEMP_DIR / job_id / "broll"
    broll_dir.mkdir(parents=True, exist_ok=True)

    for vid in results[:3]:
        vid_id  = vid["id"]
        vid_dur = vid["duration"]
        print(f"  [youtube] Trying: '{vid['title'][:50]}' ({vid_dur}s)")

        raw_path  = broll_dir / f"seg_{seg_index:02d}_raw_{vid_id}.mp4"
        trim_path = broll_dir / f"seg_{seg_index:02d}.mp4"

        if trim_path.exists() and trim_path.stat().st_size > 50_000:
            print(f"  [youtube] Using cached: {trim_path.name}")
            return trim_path

        # Try to grab the most visually active part (skip intros — start at 5s)
        start = min(5.0, max(0, vid_dur / 4))
        ok = _download_clip(vid_id, raw_path, start_sec=start, duration_sec=duration + 10)
        if not ok:
            continue

        # Trim to exact duration needed
        if _trim_to_duration(raw_path, trim_path, duration):
            raw_path.unlink(missing_ok=True)
            print(f"  [youtube] B-roll ready: {trim_path.name} ({duration:.0f}s)")
            return trim_path
        raw_path.unlink(missing_ok=True)

    print(f"  [youtube] Could not get B-roll for segment {seg_index}")
    return None


if __name__ == "__main__":
    # Quick test
    query = sys.argv[1] if len(sys.argv) > 1 else "US Iran war 2025"
    results = _search_youtube(query, max_results=5)
    print(f"Results for '{query}':")
    for r in results:
        print(f"  {r['duration']}s  {r['title'][:60]}  [{r['uploader']}]")
