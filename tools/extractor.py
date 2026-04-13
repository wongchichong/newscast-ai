"""
extractor.py — Extract embedded videos from web pages using yt-dlp
Also handles direct video file links.
"""

import json
import subprocess
import re
from pathlib import Path

from bs4 import BeautifulSoup
import requests

TEMP_DIR = Path(__file__).parent.parent / "temp"


def find_embedded_videos(html: str, base_url: str) -> list[str]:
    """Find video URLs embedded in a page (YouTube, Vimeo, direct mp4, etc.)."""
    soup = BeautifulSoup(html, "html.parser")
    videos = []

    # iframes (YouTube, Vimeo, etc.)
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if any(x in src for x in ["youtube", "vimeo", "dailymotion", "twitter", "tiktok"]):
            # Normalize YouTube embed URLs
            src = src.replace("//www.youtube.com/embed/", "//www.youtube.com/watch?v=")
            src = re.sub(r"//www\.youtube\.com/embed/([^?/]+)", r"//www.youtube.com/watch?v=\1", src)
            if not src.startswith("http"):
                src = "https:" + src
            videos.append(src)

    # Direct video tags
    for video in soup.find_all("video"):
        for src_tag in video.find_all("source"):
            src = src_tag.get("src", "")
            if src:
                if not src.startswith("http"):
                    from urllib.parse import urljoin
                    src = urljoin(base_url, src)
                videos.append(src)
        src = video.get("src", "")
        if src:
            if not src.startswith("http"):
                from urllib.parse import urljoin
                src = urljoin(base_url, src)
            videos.append(src)

    # data-video-id attributes (common in news sites)
    for tag in soup.find_all(attrs={"data-video-id": True}):
        vid_id = tag["data-video-id"]
        videos.append(f"https://www.youtube.com/watch?v={vid_id}")

    return list(dict.fromkeys(videos))  # deduplicate preserving order


def download_video(url: str, output_path: Path, max_duration_sec: int = 120) -> dict:
    """
    Download a video using yt-dlp.
    Returns dict with path and metadata.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_template = str(output_path.with_suffix("")) + ".%(ext)s"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--max-downloads", "1",
        "--match-filter", f"duration < {max_duration_sec}",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", out_template,
        "--write-info-json",
        "--no-warnings",
        url
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Find the downloaded file
    parent = output_path.parent
    downloaded = list(parent.glob("*.mp4"))
    info_files = list(parent.glob("*.info.json"))

    metadata = {}
    if info_files:
        with open(info_files[0]) as f:
            raw = json.load(f)
            metadata = {
                "title": raw.get("title", ""),
                "duration": raw.get("duration", 0),
                "description": raw.get("description", "")[:500],
                "uploader": raw.get("uploader", ""),
            }

    if downloaded:
        return {
            "success": True,
            "path": str(downloaded[0]),
            "metadata": metadata,
            "url": url,
        }
    else:
        return {
            "success": False,
            "error": result.stderr[-300:] if result.stderr else "Unknown error",
            "url": url,
        }


def extract_clip(video_path: Path, start_sec: float, end_sec: float, output_path: Path) -> Path:
    """Extract a clip from a video file using ffmpeg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end_sec - start_sec
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", str(video_path),
        "-t", str(duration),
        "-c:v", "libx264", "-c:a", "aac",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg clip error: {result.stderr[-300:]}")
    return output_path


def extract_videos_from_page(url: str, html: str, job_id: str) -> list[dict]:
    """
    Find and download all embedded videos from a page.
    Returns list of downloaded video dicts.
    """
    job_dir = TEMP_DIR / job_id / "videos"
    job_dir.mkdir(parents=True, exist_ok=True)

    video_urls = find_embedded_videos(html, url)
    print(f"[extractor] Found {len(video_urls)} embedded videos")

    results = []
    for i, vurl in enumerate(video_urls[:3]):  # limit to first 3
        print(f"  [extractor] Downloading {i+1}: {vurl[:80]}")
        out = job_dir / f"video_{i:02d}.mp4"
        result = download_video(vurl, out)
        results.append(result)
        if result["success"]:
            print(f"    → saved to {result['path']}")
        else:
            print(f"    → failed: {result.get('error','')[:80]}")

    return results


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    headers = {"User-Agent": "Mozilla/5.0"}
    html = requests.get(url, headers=headers, timeout=15).text
    results = extract_videos_from_page(url, html, "test_extract")
    print(json.dumps(results, indent=2))
