"""
search_video.py — Multi-platform video search for newscast pipeline v2.

Searches a topic across video platforms using yt-dlp:
  - YouTube
  - Bilibili
  - Dailymotion
  - Vimeo
  - And other yt-dlp supported sites

Returns structured video metadata for downstream ingestion.
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Optional
from datetime import datetime

YT_DLP = "yt-dlp"

# ── Platform definitions ─────────────────────────────────────────────────────

PLATFORMS = [
    {
        "name": "youtube",
        "prefix": "ytsearch",
        "url_template": "https://www.youtube.com/watch?v={id}",
        "max_default": 10,
    },
    {
        "name": "bilibili",
        "prefix": "bilisearch",
        "url_template": "https://www.bilibili.com/video/{id}",
        "max_default": 10,
    },
    {
        "name": "dailymotion",
        "prefix": "dmsearch",
        "url_template": "https://www.dailymotion.com/video/{id}",
        "max_default": 5,
    },
    {
        "name": "vimeo",
        "prefix": "vsearch",
        "url_template": "https://vimeo.com/{id}",
        "max_default": 5,
    },
    {
        "name": "reddit",
        "prefix": "rtsearch",
        "url_template": "https://www.reddit.com{url}",
        "max_default": 5,
    },
    {
        "name": "tiktok",
        "prefix": "tksearch",
        "url_template": "https://www.tiktok.com/@{user}/video/{id}",
        "max_default": 5,
    },
]


def _check_yt_dlp() -> bool:
    """Check if yt-dlp is installed and accessible."""
    try:
        r = subprocess.run([YT_DLP, "--version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _search_platform(query: str, prefix: str, max_results: int = 10) -> list[dict]:
    """
    Search a single platform via yt-dlp.
    Returns list of video metadata dicts.
    """
    search_query = f"{prefix}{max_results}:{query}"
    cmd = [
        YT_DLP,
        search_query,
        "--dump-json",
        "--no-playlist",
        "--flat-playlist",
        "--no-warnings",
        "--quiet",
        "--socket-timeout", "10",
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        results = []
        for line in r.stdout.strip().splitlines():
            try:
                data = json.loads(line)
                duration = data.get("duration") or 0
                # Filter: skip very short (<15s) and extremely long (>3h) videos
                if duration > 0 and (duration < 15 or duration > 10800):
                    continue

                video_url = data.get("url") or data.get("webpage_url") or ""
                results.append({
                    "id": data.get("id", ""),
                    "title": data.get("title", ""),
                    "url": video_url,
                    "duration": duration,
                    "uploader": data.get("uploader", "") or data.get("channel", ""),
                    "view_count": data.get("view_count"),
                    "upload_date": data.get("upload_date", ""),
                    "thumbnail": data.get("thumbnail", ""),
                    "description": (data.get("description", "") or "")[:300],
                })
            except json.JSONDecodeError:
                continue
        return results
    except subprocess.TimeoutExpired:
        print(f"  [video] Search timeout for query: {query}")
        return []
    except Exception as e:
        print(f"  [video] Search failed: {e}")
        return []


def _build_search_queries(topic: str) -> list[str]:
    """
    Build multiple search queries to maximize coverage.
    """
    queries = [topic]

    # Add language/time variants
    year = datetime.now().year
    queries.append(f"{topic} {year}")
    queries.append(f"{topic} news")
    queries.append(f"{topic} latest")

    # Add Chinese variant for Bilibili
    queries.append(f"{topic} 新闻")

    return queries


def search_video(
    topic: str,
    platforms: list[str] = None,
    max_per_platform: int = None,
) -> dict:
    """
    Search a topic across multiple video platforms.

    Args:
        topic: Search query / topic
        platforms: List of platform names (default: ["youtube", "bilibili", "dailymotion", "vimeo"])
        max_per_platform: Max results per platform per query (default: 10)

    Returns:
        {
            "videos": [{"id", "title", "url", "platform", "duration", "uploader", ...}],
            "total_found": int,
            "platforms_searched": list[str],
            "query": str,
        }
    """
    if not _check_yt_dlp():
        print("  [video] yt-dlp not found. Install it: pip install yt-dlp")
        return {"videos": [], "total_found": 0, "platforms_searched": [], "query": topic}

    if platforms is None:
        platforms = ["youtube", "bilibili", "dailymotion", "vimeo"]

    if max_per_platform is None:
        max_per_platform = 10

    all_videos = []
    platforms_searched = []
    queries = _build_search_queries(topic)

    for plat_name in platforms:
        plat = next((p for p in PLATFORMS if p["name"] == plat_name), None)
        if plat is None:
            print(f"  [video] Unknown platform: {plat_name}, skipping")
            continue

        print(f"  [video] Searching {plat_name} for: {topic}")
        plat_videos = []
        seen_ids = set()

        for query in queries[:3]:  # Limit to first 3 queries to avoid over-searching
            results = _search_platform(query, plat["prefix"], max_results=max_per_platform)

            for v in results:
                if v["id"] and v["id"] not in seen_ids:
                    seen_ids.add(v["id"])
                    v["platform"] = plat_name
                    v["query"] = query
                    plat_videos.append(v)

            time.sleep(1)  # Delay between queries

        platforms_searched.append(plat_name)
        all_videos.extend(plat_videos)
        print(f"  [video] {plat_name}: found {len(plat_videos)} videos")

    # Sort by relevance (prefer longer videos with more views)
    all_videos.sort(key=lambda v: (v.get("view_count") or 0) + (v.get("duration") or 0) * 0.1, reverse=True)

    result = {
        "videos": all_videos,
        "total_found": len(all_videos),
        "platforms_searched": platforms_searched,
        "query": topic,
        "timestamp": datetime.utcnow().isoformat(),
    }

    print(f"  [video] Total: {len(all_videos)} videos from {len(platforms_searched)} platforms")
    return result


# ── Extract URLs list for NotebookLM ingestion ───────────────────────────────

def get_video_urls(search_result: dict, max_urls: int = 50) -> list[str]:
    """Extract clean URL list from search results for NotebookLM source add."""
    urls = []
    for v in search_result.get("videos", []):
        url = v.get("url", "")
        if url and len(urls) < max_urls:
            urls.append(url)
    return urls


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    topic = sys.argv[1] if len(sys.argv) > 1 else "artificial intelligence news"
    result = search_video(topic)
    print(json.dumps(result, indent=2, ensure_ascii=False)[:5000])
