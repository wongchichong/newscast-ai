"""
playwright_scraper.py — Script-driven browser recording synced to narration.

Two modes:
  1. record_with_script(url, mp4, script, audio_duration)
       — Driven by key_facts + narration from the LLM script.
       — Duration matches the narration audio exactly.
       — Zooms in on page elements that match each key fact/sentence.
       — Use this after audio is already generated.

  2. playwright_scroll_video(url, mp4, duration)
       — Simple timed scroll (fallback when no script is available).

Usage:
    python3 playwright_scraper.py <URL> <output.mp4> [duration_sec]
"""

import asyncio
import re
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────

VIEWPORT_W = 1280
VIEWPORT_H = 720
OUTPUT_W   = 1920
OUTPUT_H   = 1080

HIGHLIGHT_CSS = """
.pw-zoom-target {
    outline: 4px solid rgba(255, 200, 0, 0.9) !important;
    background: rgba(255, 220, 0, 0.25) !important;
    border-radius: 4px !important;
    transition: outline 0.4s ease, background 0.4s ease !important;
}
.pw-zoom-target-fade {
    outline: none !important;
    background: transparent !important;
    transition: outline 0.5s ease, background 0.5s ease !important;
}
/* Hide cookie/consent overlays */
[id*="cookie"],[class*="cookie"],[id*="gdpr"],[class*="gdpr"],
[id*="consent"],[class*="consent"],[id*="banner"],[class*="popup"],
#onetrust-banner-sdk,.cc-window,.cookie-notice,.cookie-banner {
    display: none !important;
}
::-webkit-scrollbar { display: none; }
"""

CHROMIUM_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-extensions", "--mute-audio",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_ad_url(url: str) -> bool:
    blocklist = [
        "doubleclick", "googlesyndication", "googletagmanager", "google-analytics",
        "facebook.com/tr", "connect.facebook", "ads.", "adservice",
        "analytics.", "tracking.", "pixel.", "beacon.",
    ]
    return any(b in url for b in blocklist)


async def _launch(pw):
    return await pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)


async def _new_context(browser, webm_dir: Path):
    return await browser.new_context(
        viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
        record_video_dir=str(webm_dir),
        record_video_size={"width": VIEWPORT_W, "height": VIEWPORT_H},
        java_script_enabled=True,
        ignore_https_errors=True,
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )


async def _load_page(page, url: str):
    await page.route("**/*", lambda r: r.abort() if _is_ad_url(r.request.url) else r.continue_())
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"[playwright] load warning: {e}")
    await page.wait_for_timeout(2000)
    await page.add_style_tag(content=HIGHLIGHT_CSS)
    # Dismiss cookie banners
    for sel in [
        "button:has-text('Accept all')", "button:has-text('Accept All')",
        "button:has-text('Accept cookies')", "button:has-text('I agree')",
        "button:has-text('Agree')", "button:has-text('OK')",
        "#onetrust-accept-btn-handler", ".cc-accept",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=400):
                await btn.click(timeout=400)
                await page.wait_for_timeout(300)
                break
        except Exception:
            pass


def _webm_to_mp4(webm_dir: Path, output_mp4: Path):
    """Find the recorded webm and convert to 1920x1080 mp4."""
    webm_files = list(webm_dir.glob("*.webm"))
    if not webm_files:
        raise RuntimeError("Playwright produced no webm file")
    webm = webm_files[0]
    print(f"[playwright] WebM: {webm} ({webm.stat().st_size // 1024}KB)")

    cmd = [
        "ffmpeg", "-y", "-i", str(webm),
        "-vf",
        f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_W}:{OUTPUT_H}:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an",
        str(output_mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg error:\n{r.stderr[-500:]}")

    try:
        webm.unlink()
        webm_dir.rmdir()
    except Exception:
        pass

    print(f"[playwright] MP4: {output_mp4} ({output_mp4.stat().st_size // 1024}KB)")
    return output_mp4


# ── Element finder ────────────────────────────────────────────────────────────

async def _get_all_elements(page) -> list[dict]:
    """Return all visible text elements with their positions."""
    return await page.evaluate("""() => {
        const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,blockquote'));
        return nodes
            .filter(n => {
                const r = n.getBoundingClientRect();
                const s = window.getComputedStyle(n);
                return r.width > 100 && r.height > 0 &&
                       s.display !== 'none' && s.visibility !== 'hidden' &&
                       n.innerText && n.innerText.trim().length > 15;
            })
            .map((n, i) => ({
                idx:  i,
                tag:  n.tagName.toLowerCase(),
                top:  n.getBoundingClientRect().top + window.scrollY,
                text: n.innerText.trim().slice(0, 120),
            }))
            .filter(e => e.top > 0)
            .sort((a, b) => a.top - b.top);
    }""")


def _best_match(query: str, elements: list[dict]) -> dict | None:
    """Find the element whose text best overlaps with the query (simple word overlap)."""
    if not elements:
        return None
    query_words = set(re.sub(r'[^\w\s]', '', query.lower()).split())
    if not query_words:
        return elements[0]

    best, best_score = None, -1
    for el in elements:
        el_words = set(re.sub(r'[^\w\s]', '', el["text"].lower()).split())
        score = len(query_words & el_words) / max(len(query_words), 1)
        if score > best_score:
            best_score, best = score, el
    return best


# ── Zoom helper ───────────────────────────────────────────────────────────────

async def _zoom_to_element(page, el: dict, zoom: float = 1.6, smooth_ms: int = 600):
    """
    Scroll to element, zoom the page in around it, then highlight it.
    Uses CSS zoom on <html> so the viewport magnifies that region.
    """
    top = el["top"]
    idx = el["idx"]

    # Scroll so element is near the top of the viewport
    target_scroll = max(0, top - VIEWPORT_H * 0.2)
    await page.evaluate(f"window.scrollTo({{top: {target_scroll}, behavior: 'smooth'}})")
    await page.wait_for_timeout(smooth_ms)

    # Apply CSS zoom centered on element
    await page.evaluate(f"""(args) => {{
        const {{ zoom, smooth_ms }} = args;
        const html = document.documentElement;
        html.style.transition = `transform ${{smooth_ms}}ms ease`;
        html.style.transformOrigin = `center ${{window.scrollY + {VIEWPORT_H // 2}}}px`;
        html.style.transform = `scale(${{zoom}})`;
    }}""", {"zoom": zoom, "smooth_ms": smooth_ms})
    await page.wait_for_timeout(smooth_ms)

    # Highlight the element
    await page.evaluate("""(idx) => {
        const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,blockquote'))
            .filter(n => {
                const r = n.getBoundingClientRect();
                const s = window.getComputedStyle(n);
                return r.width > 100 && r.height > 0 &&
                       s.display !== 'none' && s.visibility !== 'hidden' &&
                       n.innerText && n.innerText.trim().length > 15;
            });
        const node = nodes[idx];
        if (node) node.classList.add('pw-zoom-target');
    }""", idx)


async def _zoom_out(page, smooth_ms: int = 500):
    """Reset zoom and remove all highlights."""
    await page.evaluate("""(smooth_ms) => {
        const html = document.documentElement;
        html.style.transition = `transform ${smooth_ms}ms ease`;
        html.style.transform = 'scale(1)';
        document.querySelectorAll('.pw-zoom-target').forEach(n => {
            n.classList.remove('pw-zoom-target');
            n.classList.add('pw-zoom-target-fade');
            setTimeout(() => n.classList.remove('pw-zoom-target-fade'), 600);
        });
    }""", smooth_ms)
    await page.wait_for_timeout(smooth_ms)


# ── Mode 1: Script-driven recording ──────────────────────────────────────────

async def _record_with_script(url: str, output_mp4: Path,
                               script: dict, audio_duration: float) -> Path:
    """
    Record browser video synced to narration audio duration.
    Zooms into page elements matching key_facts and narration sentences.
    """
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    webm_dir = output_mp4.parent / "pw_rec"
    webm_dir.mkdir(parents=True, exist_ok=True)

    # Build list of text segments to zoom to, from key_facts + narration sentences
    key_facts = script.get("key_facts", [])
    narration  = script.get("narration", "")
    # Split narration into sentences for finer targeting
    sentences  = [s.strip() for s in re.split(r'(?<=[.!?])\s+', narration) if len(s.strip()) > 20]

    # Combine: key_facts first, then sentences (deduplicated roughly)
    segments = list(key_facts) + sentences
    segments = [s for s in segments if s]

    if not segments:
        # Fallback to headline + narration chunks
        segments = [script.get("headline", ""), narration[:200]]

    n_segs = len(segments)
    # Time budget: leave 1s lead-in + 1s tail, split rest equally
    lead_in_ms  = 1500
    tail_ms     = 1000
    total_ms    = int(audio_duration * 1000)
    body_ms     = max(total_ms - lead_in_ms - tail_ms, n_segs * 1500)
    ms_per_seg  = body_ms // max(n_segs, 1)

    print(f"[playwright] Script-driven mode: {n_segs} segments × {ms_per_seg}ms = {body_ms}ms")
    print(f"[playwright] Total duration: {audio_duration:.1f}s")

    async with async_playwright() as pw:
        browser = await _launch(pw)
        context = await _new_context(browser, webm_dir)
        page    = await context.new_page()

        await _load_page(page, url)
        elements = await _get_all_elements(page)
        print(f"[playwright] Found {len(elements)} page elements")

        # Lead-in: wide view of top of page
        await page.wait_for_timeout(lead_in_ms)

        visited_idx = set()
        for seg in segments:
            el = _best_match(seg, elements)
            if el is None:
                await page.wait_for_timeout(ms_per_seg)
                continue

            # Skip if we already zoomed this element (avoid repeating same spot)
            if el["idx"] in visited_idx:
                # Still wait the time
                await page.wait_for_timeout(ms_per_seg)
                continue
            visited_idx.add(el["idx"])

            # Zoom in, hold, zoom out
            zoom_in_ms   = 600
            hold_ms      = max(ms_per_seg - zoom_in_ms - 500, 800)
            zoom_out_ms  = 500

            await _zoom_to_element(page, el, zoom=1.55, smooth_ms=zoom_in_ms)
            await page.wait_for_timeout(hold_ms)
            await _zoom_out(page, smooth_ms=zoom_out_ms)

        # Tail: zoom back out fully and show overview
        await _zoom_out(page)
        await page.wait_for_timeout(tail_ms)

        await context.close()
        await browser.close()

    return _webm_to_mp4(webm_dir, output_mp4)


# ── Mode 2: Simple timed scroll (fallback) ───────────────────────────────────

async def _record_scroll(url: str, output_mp4: Path, duration: float) -> Path:
    """Simple linear scroll — fallback when no script is available."""
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    webm_dir = output_mp4.parent / "pw_rec"
    webm_dir.mkdir(parents=True, exist_ok=True)

    print(f"[playwright] Simple scroll mode: {duration:.1f}s")

    async with async_playwright() as pw:
        browser = await _launch(pw)
        context = await _new_context(browser, webm_dir)
        page    = await context.new_page()

        await _load_page(page, url)

        total_h   = await page.evaluate("document.body.scrollHeight")
        target    = min(total_h - VIEWPORT_H, total_h * 0.85)
        step_ms   = 80
        steps     = int((duration * 1000) / step_ms)
        step_px   = target / max(steps, 1)

        # Collect elements for simple highlight-on-scroll
        elements  = await _get_all_elements(page)
        done      = set()

        await page.wait_for_timeout(1500)

        for i in range(steps):
            y = min(i * step_px, target)
            await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
            # Highlight elements that enter viewport
            vb = y + VIEWPORT_H
            for el in elements:
                if el["idx"] not in done and y <= el["top"] <= vb:
                    done.add(el["idx"])
                    await page.evaluate("""(idx) => {
                        const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,blockquote'))
                            .filter(n => n.innerText && n.innerText.trim().length > 15);
                        const node = nodes[idx];
                        if (!node) return;
                        node.classList.add('pw-zoom-target');
                        setTimeout(() => {
                            node.classList.remove('pw-zoom-target');
                            node.classList.add('pw-zoom-target-fade');
                            setTimeout(() => node.classList.remove('pw-zoom-target-fade'), 600);
                        }, 1200);
                    }""", el["idx"])
            await page.wait_for_timeout(step_ms)

        await page.wait_for_timeout(1500)
        await context.close()
        await browser.close()

    return _webm_to_mp4(webm_dir, output_mp4)


# ── Mode 3: HTML infographic page recording ───────────────────────────────────

async def _record_html_page(html_path: Path, output_mp4: Path, duration: float) -> Path:
    """
    Load a local HTML infographic (file://) and record it for `duration` seconds.
    Waits 2s for Chart.js animations to complete before the main hold.
    """
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    webm_dir = output_mp4.parent / f"pw_html_{output_mp4.stem}"
    webm_dir.mkdir(parents=True, exist_ok=True)

    url = html_path.as_uri()
    print(f"[playwright] Recording HTML infographic: {html_path.name}")

    async with async_playwright() as pw:
        browser = await _launch(pw)
        context = await browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            record_video_dir=str(webm_dir),
            record_video_size={"width": VIEWPORT_W, "height": VIEWPORT_H},
            java_script_enabled=True,
        )
        page = await context.new_page()

        # Load the local HTML file
        await page.goto(url, wait_until="networkidle", timeout=20000)

        # Wait for Chart.js animations (they run on load)
        await page.wait_for_timeout(2000)

        # Scroll slowly for timeline pages; hold still for charts
        total_h = await page.evaluate("document.body.scrollHeight")
        visible_h = VIEWPORT_H

        if total_h > visible_h * 1.3:
            # Page is taller than viewport — slow scroll
            scroll_dist = total_h - visible_h
            hold_start_ms = 1500
            scroll_duration_ms = int((duration - 3) * 1000)
            steps = max(1, scroll_duration_ms // 100)
            step_px = scroll_dist / steps
            for i in range(steps):
                y = min(i * step_px, scroll_dist)
                await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
                await page.wait_for_timeout(100)
            await page.wait_for_timeout(1500)
        else:
            # Page fits in viewport — just hold
            await page.wait_for_timeout(int(duration * 1000))

        await context.close()
        await browser.close()

    return _webm_to_mp4(webm_dir, output_mp4)


# ── Public sync wrappers ──────────────────────────────────────────────────────

def record_with_script(url: str, output_mp4: Path,
                       script: dict, audio_duration: float) -> Path:
    """Script-driven recording synced to audio duration."""
    print(f"[playwright] Recording {url}")
    print(f"[playwright] Output: {output_mp4}")
    return asyncio.run(_record_with_script(url, Path(output_mp4), script, audio_duration))


def playwright_scroll_video(url: str, output_mp4: Path, duration: float = 30) -> Path:
    """Simple scroll recording (fallback)."""
    print(f"[playwright] Recording {url}")
    print(f"[playwright] Output: {output_mp4}")
    return asyncio.run(_record_scroll(url, Path(output_mp4), duration))


def record_html_page(html_path: Path, output_mp4: Path, duration: float = 8.0) -> Path:
    """Record a local HTML infographic page for the given duration."""
    print(f"[playwright] Recording HTML: {html_path}")
    print(f"[playwright] Output: {output_mp4}")
    return asyncio.run(_record_html_page(Path(html_path), Path(output_mp4), duration))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: playwright_scraper.py <URL> <output.mp4> [duration_sec]")
        sys.exit(1)
    playwright_scroll_video(sys.argv[1], Path(sys.argv[2]),
                            float(sys.argv[3]) if len(sys.argv) > 3 else 30)
