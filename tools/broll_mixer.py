"""
broll_mixer.py — Narration-driven B-roll mixer.

Matches visuals to narration keywords:
1. Extract keywords/phrases from narration text
2. For each keyword, find matching visuals from ALL sources (stock, YouTube, article pages)
3. Display each visual for 1-5s synced to when the keyword is spoken
4. Each visual used ONCE, no repeats
5. Article pages use scroll/pan/zoom/text-select effects
6. Video/image clips use crossfade/slide/zoom transitions
"""

import json
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from youtube_search import _search_youtube, _download_clip
from stock_footage import search_stock_video, _build_stock_query

TEMP_DIR = Path(__file__).parent.parent / "temp"

# ── Keyword-to-visual mapping ────────────────────────────────────────────────

# Map common news keywords to stock search queries
KEYWORD_STOCK_MAP = {
    "chip": "computer chip semiconductor",
    "chips": "computer chip semiconductor",
    "semiconductor": "semiconductor manufacturing",
    "nvidia": "computer technology GPU",
    "gpu": "graphics card computer GPU",
    "ai": "artificial intelligence robot",
    "artificial intelligence": "AI technology computer",
    "quantum": "quantum computing technology",
    "computer": "laptop computer technology",
    "data": "data center server",
    "server": "data center servers",
    "technology": "technology digital innovation",
    "tech": "technology smartphone digital",
    "funding": "finance money investment",
    "money": "finance currency trading",
    "market": "stock market trading finance",
    "stock": "stock market trading floor",
    "trading": "finance stock market",
    "competition": "business competition rivalry",
    "rival": "business competition",
    "innovation": "innovation technology startup",
    "research": "research laboratory science",
    "scientist": "scientist laboratory research",
    "factory": "factory manufacturing production",
    "manufacturing": "manufacturing factory assembly",
    "robot": "robot automation industry",
    "robotic": "robot automation",
    "jensen": "CEO business leader speaker",
    "huang": "CEO business executive",
    "ceo": "CEO business executive meeting",
    "executive": "business executive meeting",
    "meeting": "business meeting conference",
    "conference": "conference presentation stage",
    "announcement": "press conference announcement",
    "war": "military conflict defense",
    "military": "military defense army",
    "election": "politics voting government",
    "politics": "government parliament politics",
    "government": "government building politics",
    "climate": "climate change environment",
    "weather": "weather storm environment",
    "energy": "solar energy wind power",
    "solar": "solar panel energy",
    "health": "healthcare medical hospital",
    "medical": "medical healthcare doctor",
    "hospital": "hospital healthcare medical",
    "crypto": "cryptocurrency bitcoin blockchain",
    "bitcoin": "bitcoin cryptocurrency digital",
    "blockchain": "blockchain cryptocurrency",
    "economy": "economy finance business",
    "finance": "finance banking money",
    "bank": "banking finance institution",
}

# Estimate words per second for TTS (average ~2.5 words/sec for neural TTS)
WORDS_PER_SECOND = 2.5


def _estimate_keyword_offset(text: str, keyword: str) -> float:
    """Estimate when a keyword is spoken based on its position in the text."""
    text_lower = text.lower()
    keyword_lower = keyword.lower()
    idx = text_lower.find(keyword_lower)
    if idx < 0:
        return 0.0
    # Count words before the keyword
    words_before = len(text_lower[:idx].split())
    return words_before / WORDS_PER_SECOND


def _extract_keywords(text: str) -> list[dict]:
    """
    Extract keywords from narration text and estimate when each is spoken.
    Returns: [{keyword, offset_sec, duration, search_query}]
    """
    text_lower = text.lower()
    found = []
    used_ranges = []  # Track character ranges already matched

    # 1. Try multi-word phrases first (longer matches take priority)
    for keyword, query in sorted(KEYWORD_STOCK_MAP.items(), key=lambda x: -len(x[0])):
        start = 0
        while True:
            idx = text_lower.find(keyword, start)
            if idx < 0:
                break
            # Check if this range overlaps with already found keywords
            end = idx + len(keyword)
            overlaps = any(
                idx < ur[1] and end > ur[0]
                for ur in used_ranges
            )
            if not overlaps:
                offset = _estimate_keyword_offset(text, keyword)
                found.append({
                    "keyword": keyword,
                    "offset_sec": offset,
                    "search_query": query,
                    "char_start": idx,
                    "char_end": end,
                })
                used_ranges.append((idx, end))
            start = idx + 1

    # 2. Sort by offset (when they appear in narration)
    found.sort(key=lambda x: x["offset_sec"])

    # 3. Deduplicate: keep only first occurrence of each keyword
    seen_keywords = set()
    deduplicated = []
    for kw in found:
        if kw["keyword"] not in seen_keywords:
            seen_keywords.add(kw["keyword"])
            deduplicated.append(kw)

    # 4. Assign durations: each visual gets 1-5s, but don't overlap
    result = []
    for i, kw in enumerate(deduplicated):
        next_offset = deduplicated[i + 1]["offset_sec"] if i + 1 < len(deduplicated) else None
        max_duration = (next_offset - kw["offset_sec"]) if next_offset else 5.0
        max_duration = max(1.0, min(max_duration, 5.0))
        duration = random.uniform(1.5, max_duration)

        result.append({
            "keyword": kw["keyword"],
            "offset_sec": kw["offset_sec"],
            "duration": duration,
            "search_query": kw["search_query"],
        })

    return result


# ── Visual collection per keyword ────────────────────────────────────────────

def _search_visual_for_keyword(keyword_info: dict, job_id: str, seg_index: int,
                                url: str = "", _article_cache: dict = {}) -> Optional[dict]:
    """
    For a given keyword, find ONE visual from any available source.
    Tries: stock footage -> YouTube -> article screenshot
    Returns: {path, type, duration, offset_sec}
    """
    query = keyword_info["search_query"]
    broll_dir = TEMP_DIR / job_id / "broll" / f"seg_{seg_index}_narration"
    broll_dir.mkdir(parents=True, exist_ok=True)
    keyword_safe = re.sub(r'[^a-z0-9]', '_', keyword_info["keyword"])
    target_dur = keyword_info["duration"]

    # 1. Try stock footage (1 clip per keyword, unique page)
    page = random.randint(1, 10)  # Wide page range for variety
    try:
        stock_clips = search_stock_video(query, job_id, seg_index,
                                          duration=target_dur, clip_length=max(target_dur, 4.0), page=page)
        if stock_clips:
            clip_path = stock_clips[0]
            effected = broll_dir / f"stock_{keyword_safe}_{random.choice(['zoom_in','zoom_out','pan_left','pan_right'])}.mp4"
            ok = _apply_video_effect(clip_path, effected, target_dur)
            if ok:
                return {
                    "path": effected,
                    "type": "stock",
                    "duration": target_dur,
                    "offset_sec": keyword_info["offset_sec"],
                    "keyword": keyword_info["keyword"],
                }
    except Exception as e:
        print(f"  [broll] Stock search failed for '{keyword_info['keyword']}': {e}")

    # 2. Try YouTube (1 short clip)
    try:
        results = _search_youtube(query, max_results=2)
        if results:
            r = results[0]
            vid_id = r.get("id", "")
            if vid_id:
                clip_path = broll_dir / f"yt_{keyword_safe}_{vid_id}.mp4"
                if not clip_path.exists() or clip_path.stat().st_size < 10_000:
                    start = min(5.0, max(0, r.get("duration", 60) / 4))
                    _download_clip(vid_id, clip_path, start_sec=start, duration_sec=8)
                if clip_path.exists() and clip_path.stat().st_size > 10_000:
                    effected = broll_dir / f"yt_{keyword_safe}_effected.mp4"
                    ok = _apply_video_effect(clip_path, effected, target_dur)
                    if ok:
                        return {
                            "path": effected,
                            "type": "youtube",
                            "duration": target_dur,
                            "offset_sec": keyword_info["offset_sec"],
                            "keyword": keyword_info["keyword"],
                        }
    except Exception as e:
        print(f"  [broll] YouTube search failed for '{keyword_info['keyword']}': {e}")

    # 3. Try article page screenshot/scroll if URL available
    if url:
        try:
            # Cache article recording per URL to avoid re-recording
            if url not in _article_cache:
                cache_shot = broll_dir / f"article_page_full.mp4"
                # Pass keyword for narration-driven highlighting
                ok = _record_article_segment(url, cache_shot, duration=15.0, 
                                              highlight_keyword=query)
                if ok:
                    _article_cache[url] = cache_shot
                    print(f"  [broll:article] Cached full article recording: {url}")
                else:
                    print(f"  [broll:article] Failed to record article: {url}")
                    return None

            article_full = _article_cache.get(url)
            if article_full and article_full.exists():
                # Extract a segment from the cached recording with scroll effect
                page_shot = broll_dir / f"article_{keyword_safe}.mp4"
                scroll_offset = random.randint(0, 500)  # Random scroll position
                ok = _extract_article_segment(article_full, page_shot, target_dur, scroll_offset)
                if ok:
                    return {
                        "path": page_shot,
                        "type": "article",
                        "duration": target_dur,
                        "offset_sec": keyword_info["offset_sec"],
                        "keyword": keyword_info["keyword"],
                    }
        except Exception as e:
            print(f"  [broll] Article page failed for '{keyword_info['keyword']}': {e}")

    return None


# ── Video effects ────────────────────────────────────────────────────────────

VIDEO_EFFECTS = ["crossfade", "zoom_in", "zoom_out", "slide_left", "slide_right"]
PAGE_EFFECTS = ["scroll_down", "scroll_up", "pan_left", "pan_right", "zoom_in", "zoom_out"]


def _apply_video_effect(input_path: Path, output_path: Path, duration: float,
                         effect: str = None) -> bool:
    """Apply a standard video transition effect (crossfade/zoom/slide)."""
    if effect is None:
        effect = random.choice(VIDEO_EFFECTS)

    try:
        if effect == "zoom_in":
            vf = "zoompan=z='min(zoom+0.002,1.5)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        elif effect == "zoom_out":
            vf = "zoompan=z='if(lte(zoom,1.0),1.5,max(1.001,zoom-0.002))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        elif effect == "slide_left":
            vf = "zoompan=z='1.2':x='if(lte(on,1),iw*0.2,max(0,x-2))':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        elif effect == "slide_right":
            vf = "zoompan=z='1.2':x='if(lte(on,1),0,min(x+2,iw*0.2))':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        else:  # crossfade = just the clip, transition handled at concat
            vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"

        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", vf,
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=120)
        return output_path.exists() and output_path.stat().st_size > 10_000
    except Exception:
        return False


def _apply_page_effect(input_path: Path, output_path: Path, duration: float,
                       effect: str = None) -> bool:
    """Apply page-specific effect: scroll, pan, zoom on article page capture."""
    if effect is None:
        effect = random.choice(PAGE_EFFECTS)

    try:
        if effect == "scroll_down":
            # Simulate scroll by panning down through a tall frame
            vf = f"zoompan=z='1.0':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)+{int(duration)*15}*on/{max(int(duration)*30,1)}':d=1:s=1920x1080:fps=30"
        elif effect == "scroll_up":
            vf = f"zoompan=z='1.0':x='iw/2-(iw/zoom/2)':y='max(0,ih/2-(ih/zoom/2)-{int(duration)*15}*on/{max(int(duration)*30,1)})':d=1:s=1920x1080:fps=30"
        elif effect == "pan_left":
            vf = "zoompan=z='1.2':x='if(lte(on,1),iw*0.2,max(0,x-2))':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        elif effect == "pan_right":
            vf = "zoompan=z='1.2':x='if(lte(on,1),0,min(x+2,iw*0.2))':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        elif effect == "zoom_in":
            vf = "zoompan=z='min(zoom+0.003,2.0)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        elif effect == "zoom_out":
            vf = "zoompan=z='if(lte(zoom,1.0),2.0,max(1.001,zoom-0.003))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30"
        else:
            vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"

        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", vf,
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=120)
        return output_path.exists() and output_path.stat().st_size > 10_000
    except Exception:
        return False


def _extract_article_segment(input_path: Path, output_path: Path, duration: float,
                              scroll_offset: int = 0) -> bool:
    """
    Extract a segment from a cached article recording with scroll/pan effect.
    Uses ffmpeg to crop and apply Ken Burns-style movement.
    """
    try:
        # Apply zoompan effect with varying scroll offset for each keyword
        y_offset = scroll_offset
        vf = (
            f"zoompan=z='if(lte(zoom,1.0),1.2,max(1.001,zoom-0.001))':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='max(0,{y_offset}*on/{max(int(duration)*30,1)})':"
            f"d=1:s=1920x1080:fps=30"
        )

        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", vf,
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=120)
        return output_path.exists() and output_path.stat().st_size > 10_000
    except Exception as e:
        print(f"  [broll:article] Extract segment failed: {e}")
        return False


def _record_article_segment(url: str, output_path: Path, duration: float,
                             highlight_keyword: str = "") -> bool:
    """
    Record an article page with keyword-based highlighting.
    Uses inline Playwright (not subprocess) to avoid Windows subprocess issues.
    Scrolls through the page and highlights elements matching the keyword.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import asyncio
        from playwright.async_api import async_playwright

        VIEWPORT_W, VIEWPORT_H = 1280, 720
        OUTPUT_W, OUTPUT_H = 1920, 1080
        FPS = 5

        HIGHLIGHT_CSS = """
        .kw-highlight {
            outline: 3px solid rgba(255, 200, 0, 0.9) !important;
            background: rgba(255, 220, 0, 0.2) !important;
            border-radius: 3px !important;
            transition: outline 0.3s ease, background 0.3s ease !important;
        }
        body, html { overflow: auto !important; position: static !important; }
        ::-webkit-scrollbar { display: none; }
        """

        def _is_ad_url(req_url: str) -> bool:
            blocklist = ["doubleclick", "googlesyndication", "google-analytics", "facebook.com/tr", "ads."]
            return any(b in req_url for b in blocklist)

        async def _record():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = await browser.new_context(
                    viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                    java_script_enabled=True,
                )
                page = await context.new_page()

                # Ad blocking
                await page.route("**/*", lambda r: r.abort() if _is_ad_url(r.request.url) else r.continue_())

                # Load page
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)

                # Add highlight CSS
                try:
                    await page.add_style_tag(content=HIGHLIGHT_CSS)
                except Exception:
                    pass
                await page.wait_for_timeout(200)

                # Get page height for scrolling
                try:
                    total_h = await page.evaluate("document.body.scrollHeight")
                except Exception:
                    total_h = VIEWPORT_H

                max_scroll = max(total_h - VIEWPORT_H, 0)
                total_frames = int(duration * FPS)
                frames_dir = output_path.parent / f"frames_{output_path.stem}"
                frames_dir.mkdir(parents=True, exist_ok=True)

                frame_idx = 0
                current_y = 0
                scroll_step = max(max_scroll / max(total_frames // 2, 1), 10) if max_scroll > 0 else 0

                for f in range(total_frames):
                    # Highlight keyword matches periodically
                    if highlight_keyword and f % 10 == 0:
                        try:
                            await page.evaluate(f"""(kw) => {{
                                // Remove previous highlights
                                document.querySelectorAll('.kw-highlight').forEach(el => {{
                                    el.classList.remove('kw-highlight');
                                }});
                                // Find and highlight matching text
                                const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,blockquote'));
                                for (const n of nodes) {{
                                    if (n.innerText && n.innerText.toLowerCase().includes(kw)) {{
                                        n.classList.add('kw-highlight');
                                        // Scroll to make it visible
                                        n.scrollIntoView({{block: 'center'}});
                                        break;
                                    }}
                                }}
                            }}""", highlight_keyword.lower())
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass

                    # Smooth scroll
                    if current_y < max_scroll and f < total_frames * 0.8:
                        current_y = min(current_y + scroll_step, max_scroll)

                    try:
                        await page.evaluate(f"window.scrollTo({{top: {current_y}, behavior: 'instant'}})")
                    except Exception:
                        pass

                    # Capture frame
                    try:
                        await page.screenshot(
                            path=str(frames_dir / f"frame_{frame_idx:04d}.png"),
                            full_page=False,
                            timeout=10000,
                        )
                        frame_idx += 1
                    except Exception:
                        pass

                    await page.wait_for_timeout(int(1000 / FPS))

                await browser.close()

            # Stitch frames to MP4
            frames = sorted(frames_dir.glob("frame_*.png"))
            if not frames:
                raise RuntimeError("No frames captured")

            print(f"  [broll:article] Stitching {len(frames)} frames")
            cmd = [
                "ffmpeg", "-y",
                "-framerate", str(FPS),
                "-i", str(frames_dir / "frame_%04d.png"),
                "-vf", f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,pad={OUTPUT_W}:{OUTPUT_H}:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-an", "-r", "30",
                str(output_path),
            ]
            subprocess.run(cmd, capture_output=True, timeout=60)

            # Clean up frames
            for fr in frames:
                try: fr.unlink()
                except: pass
            try: frames_dir.rmdir()
            except: pass

        asyncio.run(_record())
        return output_path.exists() and output_path.stat().st_size > 10_000

    except Exception as e:
        print(f"  [broll:article] Record failed: {e}")
        return False


# ── Main narration-driven B-roll generator ───────────────────────────────────

def gather_narration_driven_broll(segment: dict, job_id: str, seg_index: int,
                                   headline: str = "", duration: float = 15.0) -> Optional[Path]:
    """
    Generate B-roll synced to narration:
    1. Extract keywords from segment text
    2. For each keyword, find a matching visual from any source
    3. Concat visuals in narration order with transitions
    4. Each visual used ONCE
    """
    narration = segment.get("text", "")
    url = segment.get("url", "")
    broll_dir = TEMP_DIR / job_id / "broll" / f"seg_{seg_index}_narration"
    broll_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [broll] Narration text ({len(narration)} chars): {narration[:80]}...")

    # 1. Extract keywords
    keywords = _extract_keywords(narration)
    if not keywords:
        print(f"  [broll] No keywords found, falling back to generic search")
        # Fallback: use generic query
        generic_query = _build_stock_query(segment, headline)
        keywords = [{
            "keyword": "news",
            "offset_sec": 0,
            "duration": min(duration, 5.0),
            "search_query": generic_query,
        }]

    print(f"  [broll] Found {len(keywords)} keywords: {[k['keyword'] for k in keywords]}")

    # 2. Find visuals for each keyword
    visuals = []
    used_search_queries = set()  # Avoid duplicate searches

    for kw in keywords:
        query = kw["search_query"]
        # Skip if we already searched this exact query
        if query in used_search_queries:
            # Modify query slightly for variety
            query = query + " " + random.choice(["close up", "wide shot", "detail", "background"])
        used_search_queries.add(query)

        visual = _search_visual_for_keyword(
            {**kw, "search_query": query},
            job_id, seg_index, url
        )
        if visual:
            visuals.append(visual)
            print(f"  [broll] '{kw['keyword']}' -> {visual['type']} ({visual['duration']:.1f}s at {visual['offset_sec']:.1f}s)")
        else:
            print(f"  [broll] No visual found for '{kw['keyword']}'")

    if not visuals:
        print(f"  [broll] No visuals found for any keyword")
        return None

    # 3. Concat visuals in order (they're already sorted by offset)
    output_path = TEMP_DIR / job_id / "broll" / f"seg_{seg_index}_narration_final.mp4"
    ok = _concat_with_transitions([v["path"] for v in visuals], output_path)

    if ok:
        total_dur = sum(v["duration"] for v in visuals)
        print(f"  [broll] Narration-driven B-roll: {len(visuals)} visuals, {total_dur:.1f}s total")
        keyword_parts = []
        for v in visuals:
            keyword_parts.append(f"'{v['keyword']}'({v['duration']:.1f}s)")
        print(f"  [broll] Keywords: {', '.join(keyword_parts)}")
        return output_path

    return None


def _concat_with_transitions(clips: list[Path], output_path: Path) -> bool:
    """Concat clips with transitions. On Windows uses plain concat."""
    if not clips:
        return False
    if len(clips) == 1:
        shutil.copy2(clips[0], output_path)
        return output_path.exists()

    # Check if all clips have same resolution/format for safe concat
    # If not, re-encode with consistent format
    concat_list = output_path.parent / f"narration_concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{c.resolve()}'" for c in clips) + "\n",
        encoding="utf-8",
    )

    # Try concat with re-encode for safety
    try:
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264", "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ], capture_output=True, timeout=120)
        if output_path.exists() and output_path.stat().st_size > 10_000:
            return True
    except Exception as e:
        print(f"  [broll] Re-encode concat failed: {e}")

    # Fallback: copy
    try:
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(output_path),
        ], capture_output=True, timeout=60)
        if output_path.exists() and output_path.stat().st_size > 10_000:
            return True
    except Exception as e:
        print(f"  [broll] Copy concat failed: {e}")

    return False


# ── Backwards compatibility wrapper ──────────────────────────────────────────

def gather_mixed_broll(segment: dict, job_id: str, seg_index: int,
                       headline: str = "", duration: float = 15.0,
                       target_clip_count: int = 8) -> Optional[Path]:
    """Legacy wrapper - calls narration-driven broll."""
    return gather_narration_driven_broll(segment, job_id, seg_index, headline, duration)


if __name__ == "__main__":
    test_segment = {
        "type": "source_scroll",
        "text": "Nvidia chips are dominating the AI market as funding pours in",
        "country": "US",
        "url": "https://www.cnbc.com/nvidia-ai-chips",
    }
    keywords = _extract_keywords(test_segment["text"])
    print(f"Keywords: {json.dumps(keywords, indent=2)}")
