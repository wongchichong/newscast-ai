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
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    videos = []

    # 1. iframes (YouTube, Vimeo, etc.)
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if not src:
            src = iframe.get("data-src", "")
        if not src:
            continue
        # Normalize URLs
        if not src.startswith("http"):
            src = urljoin(base_url, src)
        if any(x in src.lower() for x in ["youtube", "vimeo", "dailymotion", "twitter", "tiktok", "facebook.com"]):
            # Normalize YouTube embed URLs
            src = re.sub(r"//www\.youtube\.com/embed/([^?/]+)", r"//www.youtube.com/watch?v=\1", src)
            src = re.sub(r"//youtube\.com/embed/([^?/]+)", r"//www.youtube.com/watch?v=\1", src)
            videos.append(src)

    # 2. Direct video tags
    for video in soup.find_all("video"):
        for src_tag in video.find_all("source"):
            src = src_tag.get("src") or src_tag.get("data-src", "")
            if src:
                if not src.startswith("http"):
                    src = urljoin(base_url, src)
                videos.append(src)
        src = video.get("src") or video.get("data-src", "")
        if src:
            if not src.startswith("http"):
                src = urljoin(base_url, src)
            videos.append(src)

    # 3. data-video-id attributes (common in news sites)
    for tag in soup.find_all(attrs={"data-video-id": True}):
        vid_id = tag["data-video-id"]
        if vid_id and len(vid_id) > 5:
            videos.append(f"https://www.youtube.com/watch?v={vid_id}")

    # 4. JSON-LD structured data (VideoObject)
    for script_tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script_tag.string)
            if isinstance(data, dict) and data.get("@type") == "VideoObject":
                content_url = data.get("contentUrl") or data.get("embedUrl", "")
                if content_url:
                    videos.append(content_url)
            elif isinstance(data, dict):
                # Check nested objects
                for key, val in data.items():
                    if isinstance(val, dict) and val.get("@type") == "VideoObject":
                        content_url = val.get("contentUrl") or val.get("embedUrl", "")
                        if content_url:
                            videos.append(content_url)
        except (json.JSONDecodeError, TypeError):
            continue

    # 5. Look for video URLs in data attributes or inline scripts
    video_patterns = [
        r'https?://[^\s"\']+\.mp4[^\s"\']*',
        r'https?://[^\s"\']+\.m3u8[^\s"\']*',
    ]
    for pattern in video_patterns:
        matches = re.findall(pattern, html)
        for m in matches:
            # Clean up URL
            m = m.rstrip(')"\'')
            if m not in videos:
                videos.append(m)

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
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", out_template,
        "--write-info-json",
        "--no-warnings",
        "--socket-timeout", "15",
        "--retries", "3",
        url
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    # Find the downloaded file
    parent = output_path.parent
    downloaded = list(parent.glob("*.mp4")) + list(parent.glob("*.mkv"))
    info_files = list(parent.glob("*.info.json"))

    metadata = {}
    if info_files:
        try:
            with open(info_files[0]) as f:
                raw = json.load(f)
                metadata = {
                    "title": raw.get("title", ""),
                    "duration": raw.get("duration", 0),
                    "description": raw.get("description", "")[:500],
                    "uploader": raw.get("uploader", ""),
                }
        except Exception:
            pass

    if downloaded:
        # Clean up info json
        for info_file in info_files:
            try:
                info_file.unlink()
            except Exception:
                pass
        return {
            "success": True,
            "path": str(downloaded[0]),
            "metadata": metadata,
            "url": url,
        }
    else:
        return {
            "success": False,
            "error": result.stderr[-300:] if result.stderr else "No video downloaded",
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
