"""
highlight_recorder.py — Playwright-based highlight video recording.

For each text highlight from NotebookLM:
  1. Navigate to the source page
  2. Find the highlight text on the page
  3. Record a video that scrolls/zooms/pans to the text
  4. Draw a highlight overlay on the text
  5. Save as MP4

Reuses the CSS and zoom logic from playwright_scraper.py.
"""

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────

VIEWPORT_W = 1920
VIEWPORT_H = 1080

# Reuse highlight CSS from playwright_scraper style
HIGHLIGHT_CSS = """
.pw-highlight-target {
    outline: 4px solid rgba(255, 200, 0, 0.9) !important;
    background: rgba(255, 220, 0, 0.3) !important;
    border-radius: 4px !important;
    transition: outline 0.4s ease, background 0.4s ease, transform 0.6s ease !important;
    transform: scale(1.02);
    z-index: 9999;
    position: relative;
}
.pw-highlight-target::after {
    content: '';
    position: absolute;
    inset: -8px;
    border: 2px solid rgba(255, 200, 0, 0.3);
    border-radius: 8px;
    animation: pw-pulse 1.5s ease-in-out infinite;
}
@keyframes pw-pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 0.8; }
}

/* Hide overlays, ads, modals */
[id*="cookie"],[class*="cookie"],[id*="gdpr"],[class*="gdpr"],
[id*="consent"],[class*="consent"],[id*="banner"],[class*="banner"],
[id*="popup"],[class*="popup"],[id*="modal"],[class*="modal"],
[id*="overlay"],[class*="overlay"],[id*="subscribe"],[class*="subscribe"],
[id*="newsletter"],[class*="newsletter"],[class*="ad-"],[class*="-ad"],
[class*="sponsored"],[class*="promo"],[class*="sticky-"],
[aria-modal="true"],[role="dialog"] {
    display: none !important;
    visibility: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
}
body, html {
    overflow: auto !important;
    position: static !important;
}
::-webkit-scrollbar { display: none; }
"""


# ── Find text on page ────────────────────────────────────────────────────────

def _build_find_script(text: str) -> str:
    """
    Build a JavaScript snippet that finds text on the page and returns
    the element info for highlighting.
    """
    # Escape quotes for JS string
    escaped = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')

    return f"""
(() => {{
    const searchText = "{escaped}";
    // Strategy: walk text nodes, find match, return element + offset
    const walker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
        null,
        false
    );

    let node;
    while (node = walker.nextNode()) {{
        const text = node.textContent;
        const idx = text.indexOf(searchText);
        if (idx !== -1) {{
            const el = node.parentElement;
            const rect = el.getBoundingClientRect();
            return {{
                found: true,
                tag: el.tagName,
                className: el.className,
                id: el.id,
                top: rect.top + window.scrollY,
                left: rect.left,
                width: rect.width,
                height: rect.height,
                scrollY: window.scrollY,
            }};
        }}
    }}

    // Fallback: try case-insensitive
    const walker2 = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
        null,
        false
    );

    let node2;
    while (node2 = walker2.nextNode()) {{
        const text = node2.textContent.toLowerCase();
        const searchLower = searchText.toLowerCase();
        const idx = text.indexOf(searchLower);
        if (idx !== -1) {{
            const el = node2.parentElement;
            const rect = el.getBoundingClientRect();
            return {{
                found: true,
                tag: el.tagName,
                className: el.className,
                id: el.id,
                top: rect.top + window.scrollY,
                left: rect.left,
                width: rect.width,
                height: rect.height,
                scrollY: window.scrollY,
            }};
        }}
    }}

    return {{ found: false }};
}})()
"""


def _build_highlight_script() -> str:
    """Apply highlight class to the element found by _build_find_script."""
    return """
(() => {
    const target = document.querySelector('.pw-highlight-target');
    if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
})()
"""


# ── Record single highlight ──────────────────────────────────────────────────

async def record_single_highlight(
    url: str,
    highlight_text: str,
    output_path: Path,
    duration: float = 8.0,
    highlight_duration: float = 5.0,
) -> dict:
    """
    Record a single highlight video for one page + text.

    Args:
        url: Page URL
        highlight_text: Text to highlight on the page
        output_path: Where to save the MP4
        duration: Total video duration in seconds
        highlight_duration: How long to show the highlight

    Returns:
        {"success": bool, "path": str, "found": bool}
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                "--mute-audio",
            ],
        )

        context = await browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        # Inject CSS before navigation
        await page.add_init_script(script=f"""
            const style = document.createElement('style');
            style.textContent = `{HIGHLIGHT_CSS}`;
            document.head.appendChild(style);
        """)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)  # Let page settle
        except Exception as e:
            print(f"  [highlight] Page load failed: {url} — {e}")
            await browser.close()
            return {"success": False, "found": False, "error": str(e)}

        # Find the text
        find_result = await page.evaluate(_build_find_script(highlight_text))

        if not find_result or not find_result.get("found"):
            print(f"  [highlight] Text not found on page: {highlight_text[:50]}...")
            # Fall back to simple scroll video
            await _record_simple_scroll(page, output_path, duration)
            await browser.close()
            return {"success": True, "found": False, "path": str(output_path)}

        # Scroll to the element
        scroll_y = find_result.get("top", 0) - VIEWPORT_H // 3
        await page.evaluate(f"window.scrollTo(0, {scroll_y})")
        await page.wait_for_timeout(1000)

        # Start recording
        await page.video.start_recording(str(output_path))

        # Phase 1: Show context (page overview) — 2 seconds
        await page.wait_for_timeout(2000)

        # Phase 2: Apply highlight
        await page.evaluate("""
            (() => {
                // Re-run the find script and add highlight class
                const searchText = arguments[0];
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                let node;
                while (node = walker.nextNode()) {
                    const text = node.textContent;
                    const idx = text.indexOf(searchText);
                    if (idx !== -1) {
                        const el = node.parentElement;
                        el.classList.add('pw-highlight-target');
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        return true;
                    }
                }
                // Case-insensitive fallback
                const walker2 = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                let node2;
                while (node2 = walker2.nextNode()) {
                    const text = node2.textContent.toLowerCase();
                    const searchLower = searchText.toLowerCase();
                    const idx = text.indexOf(searchLower);
                    if (idx !== -1) {
                        const el = node2.parentElement;
                        el.classList.add('pw-highlight-target');
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        return true;
                    }
                }
                return false;
            })()
        """, highlight_text)

        # Phase 3: Hold highlight — highlight_duration seconds
        await page.wait_for_timeout(int(highlight_duration * 1000))

        # Phase 4: Remove highlight, show surrounding context
        await page.evaluate("""
            document.querySelectorAll('.pw-highlight-target').forEach(el => {
                el.classList.remove('pw-highlight-target');
                el.classList.add('pw-highlight-target-fade');
            });
        """)

        # Phase 5: Remaining time
        remaining = duration - 2 - highlight_duration - 1
        if remaining > 0:
            await page.wait_for_timeout(int(remaining * 1000))

        await page.video.stop_recording()
        await browser.close()

        return {"success": True, "found": True, "path": str(output_path)}


async def _record_simple_scroll(page, output_path: Path, duration: float):
    """Fallback: simple slow scroll when text is not found."""
    await page.video.start_recording(str(output_path))

    total_scroll = duration * 100  # pixels per second
    steps = int(duration * 10)
    for _ in range(steps):
        await page.evaluate(f"window.scrollBy(0, {total_scroll // steps})")
        await page.wait_for_timeout(100)

    await page.video.stop_recording()


# ── Batch recording ──────────────────────────────────────────────────────────

async def record_highlights_batch(
    highlights: list[dict],
    output_dir: Path,
    duration_per_highlight: float = 8.0,
) -> list[dict]:
    """
    Record highlight videos for a batch of highlights.

    Args:
        highlights: List of {page_url, highlight_text, context}
        output_dir: Directory to save videos
        duration_per_highlight: Duration per highlight video

    Returns:
        List of {"success": bool, "path": str, "found": bool, "highlight_index": int}
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for i, h in enumerate(highlights):
        url = h.get("page_url", "")
        text = h.get("highlight_text", "")
        output_path = output_dir / f"highlighted_text_{i:03d}.mp4"

        text_safe = text[:50].encode('ascii', 'replace').decode('ascii')
        print(f"  [highlight] [{i+1}/{len(highlights)}] Recording: {text_safe}...")

        try:
            result = await record_single_highlight(
                url=url,
                highlight_text=text,
                output_path=output_path,
                duration=duration_per_highlight,
            )
            result["highlight_index"] = i
            result["highlight_text"] = text
            results.append(result)
        except Exception as e:
            print(f"  [highlight] Error on highlight {i}: {e}")
            results.append({
                "success": False,
                "found": False,
                "highlight_index": i,
                "highlight_text": text,
                "error": str(e),
            })

        # Small delay between recordings
        await asyncio.sleep(0.5)

    successful = [r for r in results if r.get("success")]
    print(f"  [highlight] Recorded {len(successful)}/{len(highlights)} highlights")
    return results


# ── Sync wrapper for non-async callers ────────────────────────────────────────

def record_highlights(
    highlights: list[dict],
    output_dir: Path,
    duration_per_highlight: float = 8.0,
) -> list[dict]:
    """Synchronous wrapper for record_highlights_batch."""
    return asyncio.run(record_highlights_batch(highlights, output_dir, duration_per_highlight))


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 3:
        print("Usage: python highlight_recorder.py <url> <highlight_text> [output.mp4]")
        sys.exit(1)

    url = sys.argv[1]
    text = sys.argv[2]
    output = sys.argv[3] if len(sys.argv) > 3 else "highlighted_test.mp4"

    result = asyncio.run(record_single_highlight(url, text, Path(output)))
    print(json.dumps(result, indent=2))
