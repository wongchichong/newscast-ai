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
    """Fetch article text and metadata from a URL.

    Uses requests first, then falls back to Playwright for JS-heavy pages
    where trafilatura extracts insufficient text.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NewscastBot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    # Extract clean text with trafilatura
    text = trafilatura.extract(html, include_comments=False, include_tables=True)

    # Extract metadata via BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = _extract_title(soup)
    description = _extract_description(soup)
    images = _extract_images(soup, html, resp.url)

    # If trafilatura got very little text, try Playwright for JS-rendered content
    if not text or len(text) < 200:
        print(f"  [scraper] Low text yield ({len(text) or 0} chars) — trying Playwright...")
        try:
            pw_result = _scrape_with_playwright(url, timeout=20)
            if pw_result and len(pw_result.get("text", "")) > len(text or ""):
                text = pw_result["text"]
                title = title or pw_result.get("title", "")
                html = pw_result.get("html", html)
                print(f"  [scraper] Playwright improved text to {len(text)} chars")
        except Exception as e:
            print(f"  [scraper] Playwright fallback failed: {e}")

    return {
        "url": resp.url,  # use final URL after redirects
        "title": title,
        "description": description,
        "text": text or "",
        "images": images[:5],
        "html": html,
    }


def _extract_title(soup: BeautifulSoup) -> str:
    """Extract the best title from HTML, trying multiple strategies."""
    # Priority: og:title > article headline > h1 > <title>
    selectors = [
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
        "article h1",
        "h1.headline",
        "h1.article-title",
        "h1",
        "title",
    ]
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag:
            title = tag.get("content", "") or tag.get_text(strip=True)
            if title and len(title) > 3:
                return title.strip()
    return ""


def _extract_description(soup: BeautifulSoup) -> str:
    """Extract the best description/summary from HTML."""
    selectors = [
        'meta[property="og:description"]',
        'meta[name="description"]',
        'meta[name="twitter:description"]',
        "article .summary",
        "p.deck",
        "p.subtitle",
    ]
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag:
            desc = tag.get("content", "") or tag.get_text(strip=True)
            if desc and len(desc) > 10:
                return desc.strip()
    return ""


def _extract_images(soup: BeautifulSoup, html: str, base_url: str) -> list[str]:
    """Extract relevant article images with multiple strategies."""
    from urllib.parse import urljoin

    images = []

    # 1. OG image (highest priority)
    og_image = soup.select_one('meta[property="og:image"]')
    if og_image:
        src = og_image.get("content", "")
        if src:
            images.append(src if src.startswith("http") else urljoin(base_url, src))

    # 2. Twitter card image
    twitter_img = soup.select_one('meta[name="twitter:image"]')
    if twitter_img:
        src = twitter_img.get("content", "")
        if src:
            images.append(src if src.startswith("http") else urljoin(base_url, src))

    # 3. Images inside article/content containers
    seen = set(images)
    for img in soup.select("article img, .content img, main img, .article-body img, .story-body img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if src and src.startswith("http") and src not in seen:
            # Skip tiny icons/pixels
            width = img.get("width", "")
            height = img.get("height", "")
            if width and int(width) < 50:
                continue
            images.append(src)
            seen.add(src)
            if len(images) >= 5:
                break

    return images[:5]


def _scrape_with_playwright(url: str, timeout: int = 20) -> dict | None:
    """Use Playwright to render JS-heavy pages and extract content."""
    import asyncio
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (compatible; NewscastBot/1.0)",
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            # Extract text from main content area
            text = await page.evaluate("""() => {
                const selectors = ['article', '[role="article"]', 'main', '.article-body',
                    '.story-body', '.content', '#content', '.post-content'];
                for (const s of selectors) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim().length > 200)
                        return el.innerText.trim();
                }
                // Fallback: body text minus nav/footer
                const clone = document.body.cloneNode(true);
                for (const t of ['nav', 'footer', 'aside', 'script', 'style', 'header',
                                 '.ad', '.banner', '.sidebar', '[class*="cookie"]'])
                    clone.querySelectorAll(t).forEach(n => n.remove());
                return clone.innerText.trim().slice(0, 10000);
            }""")

            title = await page.title()
            try:
                og = await page.locator('meta[property="og:title"]').get_attribute("content", timeout=500)
                if og:
                    title = og
            except Exception:
                pass

            html_content = await page.content()
            await context.close()
            await browser.close()
            return {"text": text, "title": title, "html": html_content}

    try:
        return asyncio.run(_run())
    except Exception:
        return None


def download_image(url: str, dest: Path) -> Optional[Path]:
    """Download an image to a local path. Handles redirects and validates content."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": url.split("/")[0] + "//" + url.split("/")[2] if len(url.split("/")) > 2 else "",
        }
        resp = requests.get(url, headers=headers, timeout=15, stream=True, allow_redirects=True)
        resp.raise_for_status()

        # Validate content type
        content_type = resp.headers.get("Content-Type", "")
        if content_type and not content_type.startswith("image/"):
            # Try to detect from magic bytes
            first_bytes = next(resp.iter_content(8))
            if not (first_bytes.startswith(b'\xff\xd8') or  # JPEG
                    first_bytes.startswith(b'\x89PNG') or   # PNG
                    first_bytes.startswith(b'RIFF')):       # WebP
                return None

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)

        # Validate file is not empty or too small
        if dest.stat().st_size < 1000:
            dest.unlink(missing_ok=True)
            return None

        # Convert WebP to JPEG for broader compatibility
        if url.lower().endswith(".webp") or content_type == "image/webp":
            img = Image.open(dest)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            jpeg_dest = dest.with_suffix(".jpg")
            img.save(jpeg_dest, "JPEG", quality=85)
            dest.unlink()
            return jpeg_dest

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
