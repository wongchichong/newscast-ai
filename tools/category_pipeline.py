"""
category_pipeline.py — Multi-country category-based newscast pipeline.

Usage:
    python3 category_pipeline.py --category "AI news" [--countries us,uk,cn,jp]
                                  [--duration 120] [--voice male_us] [--output out.mp4]
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))

from news_search import search_topic
from aggregator import aggregate_articles
from infographic import generate_infographics
from narrator import generate_segment_narrations, get_audio_duration
from playwright_scraper import record_with_script, record_html_page, playwright_scroll_video
from composer import (
    create_title_card, add_lower_third, add_news_ticker,
    concat_videos, get_video_duration,
)

TEMP_DIR   = Path(__file__).parent.parent / "temp"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ── Per-segment video recording ───────────────────────────────────────────────

def _record_segment(
    seg: dict,
    seg_index: int,
    html_pages: dict[str, Path],
    job_dir: Path,
) -> Path | None:
    """
    Record the visual for one narration segment.

    Returns path to the recorded .mp4, or None on failure.
    """
    seg_type = seg.get("type", "overview")
    duration  = max(seg.get("duration", 8.0), 4.0)
    out_mp4   = job_dir / "segments" / f"seg_{seg_index:02d}_raw.mp4"
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    if seg_type == "source_scroll":
        url = seg.get("url", "")
        if not url:
            print(f"  [cat-pipeline] Segment {seg_index}: no URL for source_scroll")
            return None
        print(f"  [cat-pipeline] Recording source scroll: {url[:60]}...")
        try:
            # Build a mini-script for the zoom logic
            mini_script = {
                "key_facts": [],
                "narration": seg.get("text", ""),
                "headline":  seg.get("text", "")[:80],
            }
            record_with_script(url, out_mp4, mini_script, duration)
            return out_mp4 if out_mp4.exists() else None
        except Exception as e:
            print(f"  [cat-pipeline] source_scroll recording failed ({e}), trying simple scroll...")
            try:
                playwright_scroll_video(url, out_mp4, duration=duration)
                return out_mp4 if out_mp4.exists() else None
            except Exception as e2:
                print(f"  [cat-pipeline] simple scroll also failed: {e2}")
                return None

    else:
        # infographic page
        visual_key = seg.get("visual", "overview_infographic")
        html_path  = html_pages.get(visual_key)
        if html_path is None or not html_path.exists():
            print(f"  [cat-pipeline] Segment {seg_index}: no HTML for visual '{visual_key}'")
            return None
        print(f"  [cat-pipeline] Recording infographic: {visual_key} ({duration:.1f}s)...")
        try:
            record_html_page(html_path, out_mp4, duration=duration)
            return out_mp4 if out_mp4.exists() else None
        except Exception as e:
            print(f"  [cat-pipeline] infographic recording failed: {e}")
            return None


# ── Merge audio into video ────────────────────────────────────────────────────

def _merge_segment(video: Path, audio: Path, output: Path) -> Path:
    """Merge narration audio into video, trimming/padding to match."""
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac",
        "-shortest",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"segment merge error: {r.stderr[-300:]}")
    return output


def _make_silent_visual(duration: float, output: Path) -> Path:
    """Create a black video clip of given duration (used as fallback visual)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=black:size=1920x1080:rate=25:duration={duration:.2f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"silent visual error: {r.stderr[-200:]}")
    return output


# ── Main category pipeline ────────────────────────────────────────────────────

def run_category_pipeline(
    category: str,
    countries: list[str] = None,
    max_per_country: int = 2,
    voice_key: str = "male_us",
    target_duration: int = 120,
    output_filename: str = None,
    job_id: str = None,
    llm_provider: str = None,
) -> dict:
    """
    Full category-based newscast pipeline.

    Steps:
      1. Search news across countries
      2. Aggregate + synthesize (LLM) → narration_segments + chart_data
      3. Generate HTML infographics (Chart.js)
      4. Generate per-segment narration audio (edge-tts)
      5. Record per-segment video (Playwright)
      6. Merge audio into video per segment
      7. Add lower-third overlays
      8. Concat + add title card + ticker → final MP4

    Returns dict with output_video path and metadata.
    """
    if job_id is None:
        job_id = f"cat_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    print(f"\n{'='*60}")
    print(f"NewscastAI Category Pipeline — Job: {job_id}")
    print(f"Category: {category}")
    print(f"{'='*60}\n")

    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = {"job_id": job_id, "category": category, "stages": {}}

    # ── Stage 1: Search ────────────────────────────────────────────────────────
    print("Stage 1/6: Searching news across countries...")
    try:
        articles = search_topic(
            query=category,
            countries=countries,
            max_per_country=max_per_country,
            scrape_full_text=True,
            job_dir=job_dir,
        )
        results["stages"]["search"] = {"status": "ok", "articles": len(articles)}
        print(f"  Found {len(articles)} articles from "
              f"{len(set(a['country'] for a in articles))} countries")
    except Exception as e:
        results["stages"]["search"] = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")
        articles = []

    if not articles:
        print("  WARNING: No articles found. Using minimal placeholder content.")
        articles = [{"country": "us", "country_name": "United States",
                     "title": category, "url": "", "source": "", "text": "", "html": ""}]

    # ── Stage 2: Aggregate ─────────────────────────────────────────────────────
    print("\nStage 2/6: Aggregating articles into newscast script...")
    try:
        script = aggregate_articles(
            category=category,
            articles=articles,
            target_duration=target_duration,
            provider=llm_provider,
            job_dir=job_dir,
        )
        n_segs = len(script.get("narration_segments", []))
        results["stages"]["aggregate"] = {"status": "ok", "segments": n_segs,
                                           "headline": script.get("headline", "")}
        print(f"  Headline: {script.get('headline', '')}")
        print(f"  Segments: {n_segs}")
    except Exception as e:
        results["stages"]["aggregate"] = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")
        script = {
            "headline": category.title(),
            "narration_segments": [
                {"type": "overview", "text": f"Today we look at {category} from a global perspective.",
                 "visual": "overview_infographic"},
                {"type": "closing",  "text": f"Reporting for NewscastAI Global Desk.",
                 "visual": "overview_infographic"},
            ],
            "chart_data": {"country_coverage": [], "timeline_events": [], "comparison_table": []},
            "key_facts": [],
            "lower_third_title": category.upper(),
            "lower_third_name":  "NewscastAI Global Desk",
        }

    # ── Stage 3: Infographics ──────────────────────────────────────────────────
    print("\nStage 3/6: Generating HTML infographics...")
    try:
        html_pages = generate_infographics(script, job_dir)
        results["stages"]["infographics"] = {"status": "ok", "pages": list(html_pages.keys())}
    except Exception as e:
        results["stages"]["infographics"] = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")
        html_pages = {}

    # ── Stage 4: Narration audio ───────────────────────────────────────────────
    print("\nStage 4/6: Generating per-segment narration audio...")
    try:
        narration_segments = script.get("narration_segments", [])
        segments_with_audio = generate_segment_narrations(
            narration_segments, job_id, voice_key=voice_key
        )
        n_with_audio = sum(1 for s in segments_with_audio if s.get("audio_path"))
        results["stages"]["audio"] = {"status": "ok", "segments_with_audio": n_with_audio}
        total_audio = sum(s.get("duration", 0) for s in segments_with_audio)
        print(f"  {n_with_audio}/{len(segments_with_audio)} segments with audio")
        print(f"  Total narration: {total_audio:.1f}s")
    except Exception as e:
        results["stages"]["audio"] = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")
        segments_with_audio = script.get("narration_segments", [])

    # Inject default duration for segments missing it
    for seg in segments_with_audio:
        if not seg.get("duration"):
            seg["duration"] = 8.0

    # ── Stage 5: Record videos ─────────────────────────────────────────────────
    print("\nStage 5/6: Recording per-segment videos (Playwright)...")
    videos_recorded = 0
    for i, seg in enumerate(segments_with_audio):
        seg_type = seg.get("type", "overview")
        print(f"  Segment {i+1}/{len(segments_with_audio)}: {seg_type}")
        mp4 = _record_segment(seg, i, html_pages, job_dir)
        seg["video_path"] = str(mp4) if mp4 else None
        if mp4:
            videos_recorded += 1
        else:
            print(f"    → no video for segment {i}")

    results["stages"]["video"] = {"status": "ok", "segments_recorded": videos_recorded}
    print(f"  Recorded {videos_recorded}/{len(segments_with_audio)} segment videos")

    # ── Stage 6: Compose ───────────────────────────────────────────────────────
    print("\nStage 6/6: Composing final video...")
    try:
        final_video = _compose_category(
            job_id=job_id,
            job_dir=job_dir,
            script=script,
            segments=segments_with_audio,
            output_filename=output_filename or f"newscast_{job_id}.mp4",
        )
        results["stages"]["compose"] = {"status": "ok", "output": str(final_video)}
        results["output_video"] = str(final_video)
        print(f"\n{'='*60}")
        print(f"SUCCESS! Output: {final_video}")
        print(f"{'='*60}\n")
    except Exception as e:
        results["stages"]["compose"] = {"status": "error", "error": str(e)}
        results["output_video"] = None
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()

    results["script"] = script
    return results


# ── Composition ───────────────────────────────────────────────────────────────

def _compose_category(
    job_id: str,
    job_dir: Path,
    script: dict,
    segments: list[dict],
    output_filename: str,
) -> Path:
    """Compose all segment videos into the final broadcast MP4."""
    final_output = OUTPUT_DIR / output_filename
    headline = script.get("headline", "NewscastAI")
    lower_title = script.get("lower_third_title", "GLOBAL NEWS")
    lower_name  = script.get("lower_third_name",  "NewscastAI Global Desk")

    composed_segs = []

    # 0. Title card (3s)
    title_card = job_dir / "title_card.mp4"
    print("[compose] Creating title card...")
    try:
        create_title_card(lower_title, headline, 3.0, title_card)
        composed_segs.append(title_card)
    except Exception as e:
        print(f"[compose] Title card failed: {e}")

    # 1. For each segment: merge audio, add lower third
    for i, seg in enumerate(segments):
        seg_type   = seg.get("type", "overview")
        audio_path = seg.get("audio_path")
        video_path = seg.get("video_path")
        duration   = seg.get("duration", 8.0)

        seg_base = job_dir / "segments" / f"seg_{i:02d}"

        # If no video, make a black fallback
        if not video_path or not Path(video_path).exists():
            print(f"[compose] Segment {i} ({seg_type}): no video — making black background")
            black = seg_base.parent / f"seg_{i:02d}_black.mp4"
            try:
                _make_silent_visual(duration, black)
                video_path = str(black)
            except Exception as e:
                print(f"[compose] Black fallback failed: {e}")
                continue

        merged_path = seg_base.parent / f"seg_{i:02d}_merged.mp4"

        # Merge audio if we have it
        if audio_path and Path(audio_path).exists():
            try:
                _merge_segment(Path(video_path), Path(audio_path), merged_path)
            except Exception as e:
                print(f"[compose] Segment {i} audio merge failed: {e}")
                merged_path = Path(video_path)
        else:
            merged_path = Path(video_path)

        # Add lower third
        lt_path = seg_base.parent / f"seg_{i:02d}_lt.mp4"

        # Build per-segment subtitle: country for source_scroll, type otherwise
        if seg_type == "source_scroll":
            country = seg.get("country", "")
            seg_subtitle = f"{country} — {lower_name}" if country else lower_name
        elif seg_type == "comparison":
            seg_subtitle = f"COMPARISON — {lower_name}"
        elif seg_type == "timeline":
            seg_subtitle = f"TIMELINE & BACKGROUND — {lower_name}"
        else:
            seg_subtitle = lower_name

        try:
            add_lower_third(
                merged_path, lt_path,
                title=lower_title[:50],
                subtitle=seg_subtitle[:50],
                start_sec=0.5,
                duration_sec=min(5.0, duration - 0.5),
            )
            composed_segs.append(lt_path)
        except Exception as e:
            print(f"[compose] Lower third failed for seg {i}: {e}")
            composed_segs.append(merged_path)

    if not composed_segs:
        raise RuntimeError("No segments to compose!")

    # 2. Add ticker to all segments
    tickered = []
    for i, seg_path in enumerate(composed_segs):
        tick_path = job_dir / "segments" / f"tickered_{i}.mp4"
        try:
            add_news_ticker(seg_path, tick_path, headline)
            tickered.append(tick_path)
        except Exception as e:
            print(f"[compose] Ticker failed for segment {i}: {e}")
            tickered.append(seg_path)

    # 3. Concatenate
    if len(tickered) == 1:
        shutil.copy(tickered[0], final_output)
    else:
        print(f"[compose] Concatenating {len(tickered)} segments...")
        concat_videos(tickered, final_output)

    dur = get_video_duration(final_output)
    print(f"[compose] Final video: {final_output} ({dur:.1f}s)")
    return final_output


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NewscastAI Category Pipeline")
    parser.add_argument("--category", "-c", required=True,
                        help="News category/topic (e.g. 'AI news', 'war', 'climate')")
    parser.add_argument("--countries", default=None,
                        help="Comma-separated country codes (e.g. us,uk,cn,jp). Default: all major countries.")
    parser.add_argument("--max-per-country", type=int, default=2,
                        help="Max articles per country (default: 2)")
    parser.add_argument("--duration", "-d", type=int, default=120,
                        help="Target total duration in seconds (default: 120)")
    parser.add_argument("--voice", "-v", default="male_us",
                        help="Voice key: male_us, female_us, male_uk, etc. (default: male_us)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output filename (auto-generated if not set)")
    parser.add_argument("--provider", default=None,
                        help="LLM provider override: claude, gemini, qodercli, crush, gemini-cli, claude-cli")
    parser.add_argument("--job-id", default=None,
                        help="Custom job ID (auto-generated if not set)")

    args = parser.parse_args()

    countries = args.countries.split(",") if args.countries else None

    result = run_category_pipeline(
        category=args.category,
        countries=countries,
        max_per_country=args.max_per_country,
        voice_key=args.voice,
        target_duration=args.duration,
        output_filename=args.output,
        job_id=args.job_id,
        llm_provider=args.provider,
    )

    print("\nPipeline Summary:")
    for stage, info in result["stages"].items():
        status = info.get("status", "?")
        print(f"  {stage}: {status}")
        if status == "error":
            print(f"    Error: {info.get('error', '')}")

    if result.get("output_video"):
        print(f"\nOutput: {result['output_video']}")
    else:
        print("\nPipeline completed with errors — no output video.")
        sys.exit(1)
