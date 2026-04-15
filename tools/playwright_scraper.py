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
import json as _json
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
/* ── Nuke all overlays, modals, popups, paywalls, ads ── */
[id*="cookie"],[class*="cookie"],
[id*="gdpr"],[class*="gdpr"],
[id*="consent"],[class*="consent"],
[id*="banner"],[class*="banner"],
[id*="popup"],[class*="popup"],
[id*="modal"],[class*="modal"],
[id*="overlay"],[class*="overlay"],
[id*="paywall"],[class*="paywall"],
[id*="subscribe"],[class*="subscribe"],
[id*="newsletter"],[class*="newsletter"],
[id*="ad-"],[class*="ad-"],[id*="-ad"],[class*="-ad"],
[id*="advert"],[class*="advert"],
[class*="interstitial"],[id*="interstitial"],
[class*="sticky-"],[id*="sticky-"],
[class*="fixed-bottom"],[class*="fixed-top"],
[class*="notification-bar"],[class*="alert-bar"],
#onetrust-banner-sdk,.cc-window,.cookie-notice,.cookie-banner,
.tp-modal,.tp-backdrop,.tp-iframe-wrapper,
.fancybox-overlay,.remodal-overlay,.modal-backdrop,
.ab-iam-root,.pn-widget,.push-notification,
[aria-modal="true"],[role="dialog"],[role="alertdialog"] {
    display: none !important;
    visibility: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
}
/* Restore scroll and remove fixed positioning that traps pages */
body, html {
    overflow: auto !important;
    position: static !important;
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


async def _new_context(browser, webm_dir: Path = None):
    """Create browser context. webm_dir kept for API compat but video recording disabled."""
    return await browser.new_context(
        viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
        # NO record_video_dir — video recording uses heavy threading that crashes PRoot
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
        # Use "load" to wait for redirects to settle before we touch the DOM
        await page.goto(url, wait_until="load", timeout=30000)
    except Exception as e:
        print(f"[playwright] load warning: {e}")
    # Extra wait for JS redirects (e.g. Google News RSS links)
    await page.wait_for_timeout(2500)
    try:
        await page.add_style_tag(content=HIGHLIGHT_CSS)
    except Exception:
        # Page may have redirected again; wait and retry once
        await page.wait_for_timeout(2000)
        try:
            await page.add_style_tag(content=HIGHLIGHT_CSS)
        except Exception as e:
            print(f"[playwright] style tag warning (continuing): {e}")
    # Dismiss cookie banners, login prompts, ad overlays
    dismiss_selectors = [
        # Cookie / consent
        "#onetrust-accept-btn-handler", ".cc-accept", ".cookie-accept",
        "button:has-text('Accept all')", "button:has-text('Accept All')",
        "button:has-text('Accept cookies')", "button:has-text('Accept & continue')",
        "button:has-text('I agree')", "button:has-text('Agree')",
        "button:has-text('Got it')", "button:has-text('OK')",
        "button:has-text('Continue')", "button:has-text('Dismiss')",
        # Login / subscribe walls — close buttons
        "button:has-text('Close')", "button:has-text('No thanks')",
        "button:has-text('Not now')", "button:has-text('Maybe later')",
        "button:has-text('Skip')", "button:has-text('Cancel')",
        "[aria-label='Close']", "[aria-label='close']",
        "[aria-label='Dismiss']", "[aria-label='dismiss']",
        ".modal-close", ".popup-close", ".overlay-close",
        ".close-button", ".btn-close", "#close-button",
        # Ad overlays
        "[id*='dismiss']", "[class*='dismiss']",
        "[id*='close-ad']", "[class*='close-ad']",
    ]
    for sel in dismiss_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=300):
                await btn.click(timeout=300)
                await page.wait_for_timeout(400)
                break
        except Exception:
            pass

    # One-time sweep + persistent MutationObserver for dynamically injected popups
    await page.evaluate("""() => {
        const W = window.innerWidth, H = window.innerHeight;

        function _isOverlay(el) {
            const s = window.getComputedStyle(el);
            const z = parseInt(s.zIndex) || 0;
            const pos = s.position;
            if (pos === 'fixed' || pos === 'sticky') {
                const r = el.getBoundingClientRect();
                const area = r.width * r.height;
                if (z > 10 && area > W * H * 0.15) return true;
                if (area > W * H * 0.6) return true;
            }
            if (z > 9000) return true;
            // Match by class/id keywords
            const id  = (el.id  || '').toLowerCase();
            const cls = (el.className && typeof el.className === 'string'
                          ? el.className : '').toLowerCase();
            const keywords = ['modal','overlay','popup','paywall','subscribe',
                              'cookie','gdpr','consent','interstitial','sticky',
                              'newsletter','signin','login-wall','regwall'];
            if (keywords.some(k => id.includes(k) || cls.includes(k))) {
                const s2 = window.getComputedStyle(el);
                if (s2.position === 'fixed' || s2.position === 'sticky' || z > 100)
                    return true;
            }
            return false;
        }

        function _restoreScroll() {
            document.body.style.overflow = 'auto';
            document.body.style.position = 'static';
            document.documentElement.style.overflow = 'auto';
            document.body.classList.remove(
                'modal-open','overlay-open','noscroll','no-scroll',
                'scroll-locked','body-fixed','freeze','is-modal-open'
            );
        }

        // Initial sweep
        Array.from(document.querySelectorAll('*')).forEach(el => {
            if (_isOverlay(el)) el.remove();
        });
        _restoreScroll();

        // MutationObserver: watch for dynamically injected overlays
        if (window.__pwObserver) window.__pwObserver.disconnect();
        window.__pwObserver = new MutationObserver(mutations => {
            let scrollRestored = false;
            for (const m of mutations) {
                for (const node of m.addedNodes) {
                    if (node.nodeType !== 1) continue;
                    // Check the added node and its children
                    const candidates = [node, ...node.querySelectorAll('*')];
                    for (const el of candidates) {
                        if (_isOverlay(el)) {
                            el.remove();
                            scrollRestored = false;
                        }
                    }
                }
                // Re-check body class mutations (scroll-lock)
                if (m.type === 'attributes' && m.target === document.body) {
                    scrollRestored = false;
                }
            }
            if (!scrollRestored) _restoreScroll();
        });

        window.__pwObserver.observe(document.documentElement, {
            childList:  true,
            subtree:    true,
            attributes: true,
            attributeFilter: ['class', 'style'],
        });
    }""")
    await page.wait_for_timeout(200)


def _screenshots_to_mp4(frames_dir: Path, output_mp4: Path, fps: int = 10) -> Path:
    """
    Stitch PNG screenshots into an MP4 using ffmpeg image2 muxer.
    Much more stable than Playwright video recording — no threading/encoder in browser.
    """
    frames = sorted(frames_dir.glob("frame_*.png"))
    if not frames:
        raise RuntimeError("No screenshot frames captured")

    print(f"[playwright] Stitching {len(frames)} frames → {output_mp4.name}")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(frames_dir / "frame_%04d.png"),
        "-vf",
        f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_W}:{OUTPUT_H}:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an", "-r", "30",
        str(output_mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    # Clean up frames
    for f in frames:
        try: f.unlink()
        except Exception: pass
    try: frames_dir.rmdir()
    except Exception: pass

    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg error:\n{r.stderr[-500:]}")
    print(f"[playwright] MP4: {output_mp4} ({output_mp4.stat().st_size // 1024}KB)")
    return output_mp4


# ── Element finder ────────────────────────────────────────────────────────────

async def _get_all_elements(page) -> list[dict]:
    """Return all visible text elements with positions and sizes."""
    return await page.evaluate("""() => {
        const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,blockquote,figcaption,span'));
        return nodes
            .filter(n => {
                const r = n.getBoundingClientRect();
                const s = window.getComputedStyle(n);
                return r.width > 100 && r.height > 0 &&
                       s.display !== 'none' && s.visibility !== 'hidden' &&
                       n.innerText && n.innerText.trim().length > 15;
            })
            .map((n, i) => {
                const r = n.getBoundingClientRect();
                return {
                    idx:    i,
                    tag:    n.tagName.toLowerCase(),
                    top:    r.top + window.scrollY,
                    width:  Math.round(r.width),
                    height: Math.round(r.height),
                    text:   n.innerText.trim().slice(0, 150),
                };
            })
            .filter(e => e.top > 0)
            .sort((a, b) => a.top - b.top);
    }""")


def _best_match_keywords(query: str, elements: list[dict]) -> dict | None:
    """Keyword word-overlap fallback matcher."""
    if not elements:
        return None
    query_words = set(re.sub(r'[^\w\s]', '', query.lower()).split())
    if not query_words:
        return elements[0]

    best, best_score = None, -1
    for el in elements:
        el_words = set(re.sub(r'[^\w\s]', '', el["text"].lower()).split())
        # Weight by tag importance: headings score higher
        tag_weight = 2.0 if el["tag"] in ("h1", "h2", "h3") else 1.0
        score = (len(query_words & el_words) / max(len(query_words), 1)) * tag_weight
        if score > best_score:
            best_score, best = score, el
    return best


def _llm_map_segments(segments: list[str], elements: list[dict]) -> list[int | None]:
    """
    Use LLM to semantically map each narration sentence to the most relevant
    page element index. Falls back to keyword matching if LLM unavailable.

    Returns list of element idx (or None) — one per segment.
    """
    if not elements or not segments:
        return [None] * len(segments)

    # Only send top 60 elements to keep prompt small
    el_sample = elements[:60]
    el_lines = "\n".join(
        f"{e['idx']}: [{e['tag']} {e['width']}x{e['height']}] {e['text']}"
        for e in el_sample
    )
    seg_lines = "\n".join(f"{i}: {s}" for i, s in enumerate(segments))

    prompt = (
        "You are matching spoken narration sentences to visible page elements "
        "for a news broadcast zoom effect.\n\n"
        f"PAGE ELEMENTS (index: tag WxH, text):\n{el_lines}\n\n"
        f"NARRATION SENTENCES (index: text):\n{seg_lines}\n\n"
        "For each narration sentence, choose the page element index that is most "
        "visually and semantically relevant to zoom into while that sentence is spoken.\n"
        "Prefer headings and large elements. If no element is relevant, use null.\n"
        "Return ONLY a JSON array with one integer or null per sentence.\n"
        "Example: [3, 7, null, 12, 1]\n"
        "No explanation. Raw JSON array only."
    )

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from summarizer import llm_complete
        raw = llm_complete(prompt, max_tokens=300).strip()
        # Extract JSON array
        m = re.search(r'\[[\s\S]*?\]', raw)
        if m:
            mapping = _json.loads(m.group(0))
            if len(mapping) == len(segments):
                print(f"[playwright] LLM element mapping: {mapping}")
                return mapping
    except Exception as e:
        print(f"[playwright] LLM mapping failed ({e}), using keyword fallback")

    # Fallback: keyword matching
    idx_map = {e["idx"]: e for e in elements}
    return [
        (_best_match_keywords(s, elements) or {}).get("idx")
        for s in segments
    ]


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


# ── Screenshot capture helper ─────────────────────────────────────────────────

async def _capture_frames(page, frames_dir: Path, duration: float, fps: int = 5) -> int:
    """Capture screenshots at `fps` frames/sec for `duration` seconds."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    total = int(duration * fps)
    interval_ms = int(1000 / fps)
    for i in range(total):
        try:
            await page.screenshot(
                path=str(frames_dir / f"frame_{i:04d}.png"),
                full_page=False,
            )
        except Exception:
            # Copy last frame if screenshot fails
            prev = frames_dir / f"frame_{i-1:04d}.png"
            if prev.exists():
                import shutil
                shutil.copy2(str(prev), str(frames_dir / f"frame_{i:04d}.png"))
        await page.wait_for_timeout(interval_ms)
    return total


# ── Mode 1: Script-driven recording ──────────────────────────────────────────

async def _record_with_script(url: str, output_mp4: Path,
                               script: dict, audio_duration: float) -> Path:
    """
    Screenshot-based recording: navigate page, zoom to relevant elements,
    capture frames, stitch to MP4. No Playwright video encoder = no futex crash.
    """
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = output_mp4.parent / f"frames_{output_mp4.stem}"
    FPS = 5  # 5fps is smooth enough and fast to capture

    key_facts = script.get("key_facts", [])
    narration  = script.get("narration", "")
    sentences  = [s.strip() for s in re.split(r'(?<=[.!?])\s+', narration) if len(s.strip()) > 20]
    segments   = [s for s in list(key_facts) + sentences if s]
    if not segments:
        segments = [script.get("headline", ""), narration[:200]]

    n_segs     = len(segments)
    total_ms   = int(audio_duration * 1000)
    ms_per_seg = max(1500, total_ms // max(n_segs, 1))

    print(f"[playwright] Script mode (screenshot): {n_segs} segs × {ms_per_seg}ms | {audio_duration:.1f}s")

    async with async_playwright() as pw:
        browser = await _launch(pw)
        context = await _new_context(browser)
        page    = await context.new_page()

        await _load_page(page, url)
        elements = await _get_all_elements(page)
        print(f"[playwright] Found {len(elements)} page elements")

        el_by_idx      = {e["idx"]: e for e in elements}
        mapped_indices = _llm_map_segments(segments, elements)

        frame_idx  = [0]  # mutable counter shared across captures

        async def _snap(ms: int):
            """Capture frames for `ms` milliseconds."""
            count = max(1, int(ms * FPS / 1000))
            ivl   = max(50, ms // count)
            for _ in range(count):
                try:
                    await page.screenshot(
                        path=str(frames_dir / f"frame_{frame_idx[0]:04d}.png"),
                        full_page=False,
                    )
                    frame_idx[0] += 1
                except Exception:
                    pass
                await page.wait_for_timeout(ivl)

        frames_dir.mkdir(parents=True, exist_ok=True)

        # Lead-in
        await _snap(1500)

        visited_idx = set()
        for i, seg in enumerate(segments):
            target_idx = mapped_indices[i] if i < len(mapped_indices) else None
            el = el_by_idx.get(target_idx) if target_idx is not None else None

            if el is None or el["idx"] in visited_idx:
                await _snap(ms_per_seg)
                continue
            visited_idx.add(el["idx"])

            zoom_in_ms  = 600
            hold_ms     = max(ms_per_seg - zoom_in_ms - 500, 800)
            zoom_out_ms = 400

            await _zoom_to_element(page, el, zoom=1.55, smooth_ms=zoom_in_ms)
            await _snap(zoom_in_ms)
            await _snap(hold_ms)
            await _zoom_out(page, smooth_ms=zoom_out_ms)
            await _snap(zoom_out_ms)

        await _zoom_out(page)
        await _snap(1000)

        await context.close()
        await browser.close()

    return _screenshots_to_mp4(frames_dir, output_mp4, fps=FPS)


# ── Mode 2: Simple timed scroll (fallback) ───────────────────────────────────

async def _record_scroll(url: str, output_mp4: Path, duration: float) -> Path:
    """Screenshot-based simple scroll — no video encoder."""
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = output_mp4.parent / f"frames_{output_mp4.stem}"
    FPS = 5

    print(f"[playwright] Simple scroll mode (screenshot): {duration:.1f}s")

    async with async_playwright() as pw:
        browser = await _launch(pw)
        context = await _new_context(browser)
        page    = await context.new_page()

        await _load_page(page, url)
        frames_dir.mkdir(parents=True, exist_ok=True)

        total_h = await page.evaluate("document.body.scrollHeight")
        target  = min(total_h - VIEWPORT_H, total_h * 0.85)
        total_frames = int(duration * FPS)
        ivl_ms  = int(1000 / FPS)

        for i in range(total_frames):
            y = (i / max(total_frames - 1, 1)) * max(target, 0)
            await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
            try:
                await page.screenshot(
                    path=str(frames_dir / f"frame_{i:04d}.png"),
                    full_page=False,
                )
            except Exception:
                pass
            await page.wait_for_timeout(ivl_ms)

        await context.close()
        await browser.close()

    return _screenshots_to_mp4(frames_dir, output_mp4, fps=FPS)


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
    frames_dir = output_mp4.parent / f"frames_{output_mp4.stem}"
    FPS = 5
    print(f"[playwright] Recording HTML infographic: {html_path.name}")

    async with async_playwright() as pw:
        browser = await _launch(pw)
        context = await browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            java_script_enabled=True,
            # No record_video_dir — screenshot mode
        )
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=20000)
        # Wait for Chart.js animations
        await page.wait_for_timeout(2000)
        frames_dir.mkdir(parents=True, exist_ok=True)

        total_h   = await page.evaluate("document.body.scrollHeight")
        total_frames = int(duration * FPS)
        ivl_ms    = int(1000 / FPS)

        if total_h > VIEWPORT_H * 1.3:
            scroll_dist = total_h - VIEWPORT_H
            for i in range(total_frames):
                y = (i / max(total_frames - 1, 1)) * scroll_dist
                await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
                try:
                    await page.screenshot(path=str(frames_dir / f"frame_{i:04d}.png"), full_page=False)
                except Exception:
                    pass
                await page.wait_for_timeout(ivl_ms)
        else:
            for i in range(total_frames):
                try:
                    await page.screenshot(path=str(frames_dir / f"frame_{i:04d}.png"), full_page=False)
                except Exception:
                    pass
                await page.wait_for_timeout(ivl_ms)

        await context.close()
        await browser.close()

    return _screenshots_to_mp4(frames_dir, output_mp4, fps=FPS)


# ── Subprocess isolation helpers ──────────────────────────────────────────────
# Each public wrapper runs playwright in a CHILD process so that a Chromium
# crash / futex abort cannot corrupt the parent process's kernel state.

import json as _json
import os as _os

_SELF = Path(__file__).resolve()


def _run_in_subprocess(args: list, timeout: float = 300) -> bool:
    """
    Spawn: python3 playwright_scraper.py <args...>
    Streams stdout/stderr to parent.  Returns True on success.
    """
    cmd = [sys.executable, str(_SELF)] + [str(a) for a in args]
    r = subprocess.run(cmd, timeout=timeout + 30)
    return r.returncode == 0


# ── Public sync wrappers ──────────────────────────────────────────────────────

def record_with_script(url: str, output_mp4: Path,
                       script: dict, audio_duration: float) -> Path:
    """Script-driven recording synced to audio duration — runs in subprocess."""
    output_mp4 = Path(output_mp4)
    print(f"[playwright] Recording {url}")
    print(f"[playwright] Output: {output_mp4}")
    # Write script to a temp JSON file next to the output
    script_json = output_mp4.with_suffix(".script.json")
    script_json.write_text(_json.dumps(script))
    try:
        ok = _run_in_subprocess(
            ["script", url, str(output_mp4), str(script_json), str(audio_duration)],
            timeout=audio_duration + 60,
        )
    finally:
        script_json.unlink(missing_ok=True)
    if not ok or not output_mp4.exists():
        raise RuntimeError("[Errno 38] Function not implemented" if not ok else "No output produced")
    return output_mp4


def playwright_scroll_video(url: str, output_mp4: Path, duration: float = 30) -> Path:
    """Simple scroll recording (fallback) — runs in subprocess."""
    output_mp4 = Path(output_mp4)
    print(f"[playwright] Recording {url}")
    print(f"[playwright] Output: {output_mp4}")
    ok = _run_in_subprocess(["scroll", url, str(output_mp4), str(duration)],
                            timeout=duration + 60)
    if not ok or not output_mp4.exists():
        raise RuntimeError("Playwright scroll recording failed")
    return output_mp4


def record_html_page(html_path: Path, output_mp4: Path, duration: float = 8.0) -> Path:
    """Record a local HTML infographic page — runs in subprocess."""
    output_mp4 = Path(output_mp4)
    print(f"[playwright] Recording HTML: {html_path}")
    print(f"[playwright] Output: {output_mp4}")
    ok = _run_in_subprocess(["html", str(html_path), str(output_mp4), str(duration)],
                            timeout=duration + 60)
    if not ok or not output_mp4.exists():
        raise RuntimeError("Playwright HTML recording failed")
    return output_mp4


if __name__ == "__main__":
    # Called by subprocess wrappers above.
    # argv[1] = mode: scroll | script | html
    if len(sys.argv) < 4:
        print("Usage: playwright_scraper.py scroll <URL> <output.mp4> [duration]")
        print("       playwright_scraper.py script <URL> <output.mp4> <script.json> <duration>")
        print("       playwright_scraper.py html   <html_path> <output.mp4> [duration]")
        sys.exit(1)

    import os as _os_main
    mode = sys.argv[1]

    try:
        if mode == "scroll":
            url      = sys.argv[2]
            out      = Path(sys.argv[3])
            dur      = float(sys.argv[4]) if len(sys.argv) > 4 else 30
            asyncio.run(_record_scroll(url, out, dur))

        elif mode == "script":
            url         = sys.argv[2]
            out         = Path(sys.argv[3])
            script_path = Path(sys.argv[4])
            dur         = float(sys.argv[5]) if len(sys.argv) > 5 else 60
            script_data = _json.loads(script_path.read_text())
            asyncio.run(_record_with_script(url, out, script_data, dur))

        elif mode == "html":
            html_p = Path(sys.argv[2])
            out    = Path(sys.argv[3])
            dur    = float(sys.argv[4]) if len(sys.argv) > 4 else 8.0
            asyncio.run(_record_html_page(html_p, out, dur))

        else:
            print(f"Unknown mode: {mode}")
            _os_main._exit(1)

        # Skip Python/asyncio cleanup — it triggers [Errno 38] on PRoot
        _os_main._exit(0)

    except BaseException as _e:
        print(f"[playwright] Error: {_e}")
        _os_main._exit(1)
