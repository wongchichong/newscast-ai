"""
scraper.py — Web page capture with scroll/zoom/highlight animation
Produces a video clip of the page being browsed (Ken Burns effect on screenshots)
"""

import asyncio
import os
import subprocess
import json
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFilter
import trafilatura


TEMP_DIR = Path(__file__).parent.parent / "temp"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


def fetch_article(url: str) -> dict:
    """Fetch article text and metadata from a URL."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewscastBot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # Extract clean text
    text = trafilatura.extract(html, include_comments=False, include_tables=False)

    # Extract metadata via BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    for sel in ["h1", 'meta[property="og:title"]', "title"]:
        tag = soup.select_one(sel)
        if tag:
            title = tag.get("content", "") or tag.get_text(strip=True)
            if title:
                break

    description = ""
    meta_desc = soup.select_one('meta[name="description"], meta[property="og:description"]')
    if meta_desc:
        description = meta_desc.get("content", "")

    # Extract image URLs
    images = []
    for img in soup.select("article img, .content img, main img")[:8]:
        src = img.get("src") or img.get("data-src", "")
        if src and src.startswith("http"):
            images.append(src)

    og_image = soup.select_one('meta[property="og:image"]')
    if og_image:
        images.insert(0, og_image.get("content", ""))

    return {
        "url": url,
        "title": title,
        "description": description,
        "text": text or "",
        "images": images[:5],
        "html": html,
    }


def download_image(url: str, dest: Path) -> Optional[Path]:
    """Download an image to a local path."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return dest
    except Exception as e:
        print(f"  [scraper] image download failed: {e}")
        return None


def create_highlight_frame(img_path: Path, highlight_box: tuple = None) -> Path:
    """Add a highlight overlay to an image frame."""
    img = Image.open(img_path).convert("RGBA")
    if highlight_box:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        x1, y1, x2, y2 = highlight_box
        draw.rectangle([x1, y1, x2, y2], outline=(255, 220, 0, 255), width=6)
        # Semi-transparent yellow fill
        draw.rectangle([x1 + 3, y1 + 3, x2 - 3, y2 - 3], fill=(255, 220, 0, 40))
        img = Image.alpha_composite(img, overlay)
    out = img_path.with_suffix(".highlighted.png")
    img.convert("RGB").save(out)
    return out


def build_scroll_video(images: list[Path], output_path: Path, duration_per_image: float = 3.0) -> Path:
    """
    Build a video from a list of images using Ken Burns (scroll/pan/zoom) effect via ffmpeg.
    Each image gets a slow pan or zoom to simulate reading/scrolling.
    """
    if not images:
        raise ValueError("No images provided for scroll video")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write ffmpeg concat file
    concat_file = TEMP_DIR / "concat.txt"
    # Resize all images to 1920x1080 first
    resized = []
    for i, img_path in enumerate(images):
        out = TEMP_DIR / f"frame_{i:03d}.jpg"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(img_path),
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
            str(out)
        ], capture_output=True)
        resized.append(out)

    # Build Ken Burns filter for each image
    filter_parts = []
    inputs = []
    for i, img in enumerate(resized):
        inputs += ["-loop", "1", "-t", str(duration_per_image), "-i", str(img)]
        fps = 25
        frames = int(duration_per_image * fps)
        # Alternate between zoom-in and pan-right
        if i % 2 == 0:
            # Zoom in from 1.0 to 1.05
            zoom_expr = f"1.0+0.05*on/{frames}"
            vf = f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080:fps={fps}"
        else:
            # Pan right
            vf = f"zoompan=z=1.02:x='min(iw*0.02*on/{frames},iw*0.02)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080:fps={fps}"
        filter_parts.append(f"[{i}:v]{vf}[v{i}]")

    concat_inputs = "".join(f"[v{i}]" for i in range(len(resized)))
    filter_parts.append(f"{concat_inputs}concat=n={len(resized)}:v=1:a=0[out]")
    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {result.stderr[-500:]}")
    return output_path


def scrape_text_only(url: str, job_id: str) -> dict:
    """
    Fetch article text + metadata only — no video recording.
    The pipeline records video later (after audio), synced to narration.
    """
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    print(f"[scraper] Fetching {url}")
    article = fetch_article(url)

    with open(job_dir / "article.json", "w") as f:
        json.dump({k: v for k, v in article.items() if k != "html"}, f, indent=2)

    img_paths = []
    for i, img_url in enumerate(article["images"]):
        dest = job_dir / f"img_{i:02d}.jpg"
        path = download_image(img_url, dest)
        if path and path.exists():
            img_paths.append(path)

    return {
        "title":       article["title"],
        "description": article["description"],
        "text":        article["text"] or "",
        "html":        article["html"],
        "images":      [str(p) for p in img_paths],
        "job_dir":     str(job_dir),
    }


def scrape_to_video(url: str, job_id: str) -> dict:
    """
    Full pipeline: fetch page → download images → build scroll video.
    Returns dict with article data + path to scroll video.
    """
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    print(f"[scraper] Fetching {url}")
    article = fetch_article(url)

    # Save article metadata
    with open(job_dir / "article.json", "w") as f:
        json.dump({k: v for k, v in article.items() if k != "html"}, f, indent=2)

    # Download images
    img_paths = []
    for i, img_url in enumerate(article["images"]):
        dest = job_dir / f"img_{i:02d}.jpg"
        path = download_image(img_url, dest)
        if path and path.exists():
            img_paths.append(path)
            print(f"  [scraper] downloaded image {i+1}/{len(article['images'])}")

    scroll_video = None
    # Prefer Playwright browser recording; fall back to ffmpeg Ken Burns
    try:
        from playwright_scraper import playwright_scroll_video
        scroll_video = job_dir / "scroll.mp4"
        print(f"[scraper] Recording browser scroll with Playwright...")
        playwright_scroll_video(url, scroll_video, duration=30)
        print(f"[scraper] Scroll video (Playwright): {scroll_video}")
    except Exception as e:
        print(f"[scraper] Playwright recording failed ({e}), falling back to image scroll")
        if img_paths:
            scroll_video = job_dir / "scroll.mp4"
            print(f"[scraper] Building scroll video from {len(img_paths)} images...")
            build_scroll_video(img_paths, scroll_video)
            print(f"[scraper] Scroll video: {scroll_video}")

    return {
        "title": article["title"],
        "description": article["description"],
        "text": article["text"],
        "images": [str(p) for p in img_paths],
        "scroll_video": str(scroll_video) if scroll_video else None,
        "job_dir": str(job_dir),
    }


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    result = scrape_to_video(url, "test_job")
    print(json.dumps({k: v for k, v in result.items() if k != "text"}, indent=2))
