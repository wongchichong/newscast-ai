"""
category_pipeline.py — Multi-country category-based newscast pipeline.

Usage:
    python3 category_pipeline.py --category "AI news" [--countries us,uk,cn,jp]
                                  [--duration 120] [--voice male_us] [--output out.mp4]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Disable ONNX runtime thread affinity which breaks in PRoot containers
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

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


# ── Resume helpers ───────────────────────────────────────────────────────────

def list_jobs(temp_dir: Path = None) -> list[dict]:
    """
    Return all jobs in temp_dir, sorted newest first.
    Each entry: {job_id, category, status, stages_done, created, output_exists}
    """
    base = temp_dir or TEMP_DIR
    jobs = []
    for job_dir in sorted(base.glob("cat_*"), reverse=True):
        ckpt = _load_checkpoint(job_dir)
        if not ckpt:
            continue
        args = ckpt.get("_args", {})
        stages_done = [k.replace("_done", "") for k, v in ckpt.items()
                       if k.endswith("_done") and v]
        output = OUTPUT_DIR / f"newscast_{job_dir.name}.mp4"
        jobs.append({
            "job_id":       job_dir.name,
            "category":     args.get("category", "?"),
            "countries":    args.get("countries"),
            "stages_done":  stages_done,
            "complete":     output.exists(),
            "output":       str(output) if output.exists() else None,
            "job_dir":      str(job_dir),
        })
    return jobs


def find_latest_incomplete_job(category: str = None) -> str | None:
    """Find the most recent job that has a checkpoint but no output video."""
    for job in list_jobs():
        if job["complete"]:
            continue
        if category and job["category"].lower() != category.lower():
            continue
        return job["job_id"]
    return None


# ── Chart data quality check ─────────────────────────────────────────────────

def _has_rich_chart_data(script: dict) -> bool:
    """
    Return True only if the LLM-generated chart_data has enough substance
    to produce a meaningful infographic (not empty/placeholder data).
    """
    cd = script.get("chart_data", {})
    coverage   = cd.get("country_coverage", [])
    timeline   = cd.get("timeline_events", [])
    comparison = cd.get("comparison_table", [])

    if len(coverage) < 2:
        return False
    if len(timeline) < 2:
        return False
    if not comparison:
        return False

    # Check that comparison rows have real (non-placeholder) values
    real_values = 0
    for row in comparison:
        for k, v in row.items():
            if k != "aspect" and v and v not in ("...", "N/A", "?", ""):
                real_values += 1
    if real_values < 3:
        return False

    return True


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def _ckpt_path(job_dir: Path) -> Path:
    return job_dir / "checkpoint.json"


def _load_checkpoint(job_dir: Path) -> dict:
    p = _ckpt_path(job_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_checkpoint(job_dir: Path, ckpt: dict):
    _ckpt_path(job_dir).write_text(json.dumps(ckpt, indent=2, default=str))


# ── Per-segment video recording ───────────────────────────────────────────────

def _record_segment(
    seg: dict,
    seg_index: int,
    html_pages: dict[str, Path],
    job_dir: Path,
    script: dict | None = None,
    job_id: str = "",
) -> Path | None:
    """
    Record the visual for one narration segment.

    Priority:
      source_scroll → 1) article page scroll  2) YouTube B-roll  3) avatar fallback
      infographic   → 1) HTML infographic      2) YouTube B-roll  3) avatar fallback
      other         → 1) HTML infographic      2) avatar fallback

    Returns path to the recorded .mp4, or None on failure.
    """
    from youtube_search import find_broll_for_segment
    from avatar_page import record_avatar

    seg_type = seg.get("type", "overview")
    duration = max(seg.get("duration", 8.0), 4.0)
    out_mp4  = job_dir / "segments" / f"seg_{seg_index:02d}_raw.mp4"
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    headline = (script or {}).get("headline", "")
    seg["_index"] = seg_index  # for avatar naming

    def _try_article_page() -> bool:
        url = seg.get("url", "")
        if not url:
            return False
        print(f"  [cat-pipeline] Recording article page: {url[:60]}...")
        try:
            mini_script = {
                "key_facts": [],
                "narration": seg.get("text", ""),
                "headline":  seg.get("text", "")[:80],
            }
            record_with_script(url, out_mp4, mini_script, duration)
            return out_mp4.exists() and out_mp4.stat().st_size > 20_000
        except Exception as e:
            print(f"  [cat-pipeline] article scroll failed ({e}), trying simple scroll...")
            try:
                playwright_scroll_video(url, out_mp4, duration=duration)
                return out_mp4.exists() and out_mp4.stat().st_size > 20_000
            except Exception as e2:
                print(f"  [cat-pipeline] simple scroll also failed: {e2}")
                return False

    def _try_infographic() -> bool:
        visual_key = seg.get("visual", "overview_infographic")
        html_path  = html_pages.get(visual_key)
        if html_path is None or not html_path.exists():
            return False
        print(f"  [cat-pipeline] Recording infographic: {visual_key} ({duration:.1f}s)...")
        try:
            record_html_page(html_path, out_mp4, duration=duration)
            return out_mp4.exists() and out_mp4.stat().st_size > 10_000
        except Exception as e:
            print(f"  [cat-pipeline] infographic recording failed: {e}")
            return False

    def _try_youtube() -> bool:
        print(f"  [cat-pipeline] Trying YouTube B-roll for segment {seg_index}...")
        broll = find_broll_for_segment(seg, job_id, seg_index,
                                       headline=headline, duration=duration)
        if broll and broll.exists():
            import shutil
            shutil.copy2(str(broll), str(out_mp4))
            return out_mp4.stat().st_size > 20_000
        return False

    def _try_avatar() -> bool:
        print(f"  [cat-pipeline] Using avatar fallback for segment {seg_index}...")
        result = record_avatar(seg, script or {}, out_mp4, duration=duration, job_id=job_id)
        return result is not None and out_mp4.exists()

    # Try strategies in order
    if seg_type == "source_scroll":
        if _try_article_page():
            return out_mp4
        if _try_youtube():
            return out_mp4
        if _try_avatar():
            return out_mp4
    else:
        if _try_infographic():
            return out_mp4
        if _try_youtube():
            return out_mp4
        if _try_avatar():
            return out_mp4

    print(f"  [cat-pipeline] All strategies failed for segment {seg_index}")
    return None


# ── Subprocess-isolated segment recording ────────────────────────────────────

def _run_worker(strategy: str, seg: dict, seg_index: int,
                html_pages: dict, job_dir: Path,
                script: dict | None = None, job_id: str = "",
                timeout: float = 360) -> Path | None:
    """
    Spawn a single-strategy worker subprocess.
    Each call is a fresh process — Playwright crashes can't accumulate.
    Returns Path to mp4 if successful, None otherwise.
    """
    _self = Path(__file__)
    result_file = job_dir / f"seg_{seg_index:02d}_{strategy}_result.json"
    seg_file    = job_dir / f"seg_{seg_index:02d}_{strategy}_seg.json"
    script_file = job_dir / f"seg_{seg_index:02d}_{strategy}_script.json"
    html_map    = {k: str(v) for k, v in html_pages.items()}

    try:
        seg_file.write_text(json.dumps(seg, default=str))
        script_file.write_text(json.dumps(script or {}, default=str))
    except OSError as e:
        print(f"  [worker] Could not write worker files: {e}")
        return None

    cmd = [
        sys.executable, str(_self),
        "--_worker_segment",
        str(seg_index),
        str(seg_file),
        str(script_file),
        json.dumps(html_map),
        str(job_dir),
        job_id,
        str(result_file),
        strategy,  # passed to worker so it only tries one strategy
    ]

    try:
        subprocess.run(cmd, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        print(f"  [worker:{strategy}] Segment {seg_index} timed out")
    except OSError as e:
        print(f"  [worker:{strategy}] Segment {seg_index} OSError (PRoot): {e}")
    except Exception as e:
        print(f"  [worker:{strategy}] Segment {seg_index} error: {e}")
    finally:
        for _f in (seg_file, script_file):
            try: _f.unlink(missing_ok=True)
            except OSError: pass

    # Check expected output location first (written before crash)
    expected_mp4 = job_dir / "segments" / f"seg_{seg_index:02d}_raw.mp4"
    if expected_mp4.exists() and expected_mp4.stat().st_size > 10_000:
        try: result_file.unlink(missing_ok=True)
        except OSError: pass
        return expected_mp4

    # Read result file
    try:
        if result_file.exists():
            data = json.loads(result_file.read_text())
            try: result_file.unlink(missing_ok=True)
            except OSError: pass
            mp4 = Path(data["mp4"]) if data.get("mp4") else None
            if mp4 and mp4.exists() and mp4.stat().st_size > 10_000:
                return mp4
    except Exception:
        pass
    return None


def _record_segment_isolated(
    seg: dict,
    seg_index: int,
    html_pages: dict,
    job_dir: Path,
    script: dict | None = None,
    job_id: str = "",
) -> Path | None:
    """
    Cascade through strategies, each in its own subprocess.
    A Playwright crash in one subprocess cannot affect the next.
    """
    seg_type = seg.get("type", "overview")

    strategies = (
        ["article", "youtube", "avatar"] if seg_type == "source_scroll"
        else ["infographic", "youtube", "avatar"]
    )

    for strategy in strategies:
        print(f"  [worker] Segment {seg_index} trying strategy: {strategy}")
        mp4 = _run_worker(strategy, seg, seg_index, html_pages, job_dir,
                          script=script, job_id=job_id)
        if mp4:
            return mp4

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

    ckpt = _load_checkpoint(job_dir)
    if ckpt:
        print(f"[resume] Checkpoint found — resuming from last completed stage.")
        # Restore args from checkpoint if not provided
        if category == ckpt.get("_args", {}).get("category", category):
            saved = ckpt.get("_args", {})
            if countries is None and saved.get("countries"):
                countries = saved["countries"]
            if voice_key == "male_us" and saved.get("voice_key"):
                voice_key = saved["voice_key"]
            if target_duration == 120 and saved.get("target_duration"):
                target_duration = saved["target_duration"]

    # Save run args into checkpoint so --resume can reconstruct them
    if not ckpt.get("_args"):
        ckpt["_args"] = {
            "category":        category,
            "countries":       countries,
            "voice_key":       voice_key,
            "target_duration": target_duration,
            "max_per_country": max_per_country,
        }
        _save_checkpoint(job_dir, ckpt)

    results = {"job_id": job_id, "category": category, "stages": {}}

    # ── Stage 1: Search ────────────────────────────────────────────────────────
    if ckpt.get("search_done") and ckpt.get("articles"):
        articles = ckpt["articles"]
        print(f"Stage 1/6: Search — skipped (cached {len(articles)} articles)")
    else:
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
            ckpt["search_done"] = True
            ckpt["articles"] = articles
            _save_checkpoint(job_dir, ckpt)
        except Exception as e:
            results["stages"]["search"] = {"status": "error", "error": str(e)}
            print(f"  ERROR: {e}")
            articles = []

    if not articles:
        print("  WARNING: No articles found. Using minimal placeholder content.")
        articles = [{"country": "us", "country_name": "United States",
                     "title": category, "url": "", "source": "", "text": "", "html": ""}]

    # ── Stage 2: Aggregate ─────────────────────────────────────────────────────
    script_json = job_dir / "aggregated_script.json"
    if ckpt.get("aggregate_done") and script_json.exists():
        script = json.loads(script_json.read_text())
        print(f"Stage 2/6: Aggregate — skipped (cached: {script.get('headline', '')})")
    else:
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
            ckpt["aggregate_done"] = True
            _save_checkpoint(job_dir, ckpt)
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

    # ── Stage 3: Infographics (optional — only if chart data is rich enough) ──────
    infographics_dir = job_dir / "infographics"
    if ckpt.get("infographics_done") and infographics_dir.exists():
        key_map = {"overview": "overview_infographic", "comparison": "comparison_chart",
                   "timeline": "timeline_infographic"}
        html_pages = {key_map.get(p.stem, p.stem): p for p in infographics_dir.glob("*.html")}
        print(f"Stage 3/6: Infographics — skipped (cached {len(html_pages)} pages)")
    elif _has_rich_chart_data(script):
        print("\nStage 3/6: Generating HTML infographics...")
        try:
            html_pages = generate_infographics(script, job_dir)
            results["stages"]["infographics"] = {"status": "ok", "pages": list(html_pages.keys())}
            ckpt["infographics_done"] = True
            _save_checkpoint(job_dir, ckpt)
        except Exception as e:
            results["stages"]["infographics"] = {"status": "error", "error": str(e)}
            print(f"  ERROR: {e}")
            html_pages = {}
    else:
        print("\nStage 3/6: Infographics — skipped (chart data too sparse, will use article pages instead)")
        results["stages"]["infographics"] = {"status": "skipped", "reason": "sparse chart data"}
        html_pages = {}

    # ── Stage 4: Narration audio ───────────────────────────────────────────────
    narration_segments = script.get("narration_segments", [])
    if ckpt.get("audio_done") and ckpt.get("segments_with_audio"):
        segments_with_audio = ckpt["segments_with_audio"]
        # Restore Path objects for audio_path
        for seg in segments_with_audio:
            if seg.get("audio_path"):
                seg["audio_path"] = str(seg["audio_path"])
        total_audio = sum(s.get("duration", 0) for s in segments_with_audio)
        print(f"Stage 4/6: Narration — skipped (cached {len(segments_with_audio)} segments, "
              f"{total_audio:.1f}s total)")
    else:
        print("\nStage 4/6: Generating per-segment narration audio...")
        try:
            segments_with_audio = generate_segment_narrations(
                narration_segments, job_id, voice_key=voice_key
            )
            n_with_audio = sum(1 for s in segments_with_audio if s.get("audio_path"))
            results["stages"]["audio"] = {"status": "ok", "segments_with_audio": n_with_audio}
            total_audio = sum(s.get("duration", 0) for s in segments_with_audio)
            print(f"  {n_with_audio}/{len(segments_with_audio)} segments with audio")
            print(f"  Total narration: {total_audio:.1f}s")
            ckpt["audio_done"] = True
            ckpt["segments_with_audio"] = segments_with_audio
            _save_checkpoint(job_dir, ckpt)
        except Exception as e:
            results["stages"]["audio"] = {"status": "error", "error": str(e)}
            print(f"  ERROR: {e}")
            segments_with_audio = narration_segments

    # Inject default duration for segments missing it
    for seg in segments_with_audio:
        if not seg.get("duration"):
            seg["duration"] = 8.0

    # ── Stage 5: Record videos ─────────────────────────────────────────────────
    print("\nStage 5/6: Recording per-segment videos (Playwright)...")
    recorded_segs = ckpt.get("recorded_segments", {})  # {str(i): video_path}
    videos_recorded = 0

    for i, seg in enumerate(segments_with_audio):
        seg_type = seg.get("type", "overview")

        # Check if this segment's raw video already exists
        existing_raw = job_dir / "segments" / f"seg_{i:02d}_raw.mp4"
        if str(i) in recorded_segs or (existing_raw.exists() and existing_raw.stat().st_size > 10000):
            vpath = recorded_segs.get(str(i)) or str(existing_raw)
            seg["video_path"] = vpath
            videos_recorded += 1
            print(f"  Segment {i+1}/{len(segments_with_audio)}: {seg_type} — skipped (cached)")
            continue

        print(f"  Segment {i+1}/{len(segments_with_audio)}: {seg_type}")
        mp4 = None
        try:
            mp4 = _record_segment_isolated(seg, i, html_pages, job_dir, script=script, job_id=job_id)
        except OSError as e:
            print(f"  [worker] Segment {i} parent OSError (PRoot): {e} — skipping segment")
        except Exception as e:
            print(f"  [worker] Segment {i} unexpected error: {e} — skipping segment")
        seg["video_path"] = str(mp4) if mp4 else None
        if mp4:
            videos_recorded += 1
            recorded_segs[str(i)] = str(mp4)
            ckpt["recorded_segments"] = recorded_segs
            _save_checkpoint(job_dir, ckpt)
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
        ckpt["compose_done"] = True
        ckpt["output_video"] = str(final_video)
        _save_checkpoint(job_dir, ckpt)
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

    # ── Worker mode: called by _record_segment_isolated ───────────────────────
    # Usage: category_pipeline.py --_worker_segment <idx> <seg.json> <script.json>
    #        <html_map_json> <job_dir> <job_id> <result.json>
    if len(sys.argv) > 1 and sys.argv[1] == "--_worker_segment":
        import os as _os
        _idx        = int(sys.argv[2])
        _seg        = json.loads(Path(sys.argv[3]).read_text())
        _script     = json.loads(Path(sys.argv[4]).read_text())
        _html_map   = json.loads(sys.argv[5])
        _job_dir    = Path(sys.argv[6])
        _job_id     = sys.argv[7]
        _result_f   = Path(sys.argv[8])
        _strategy   = sys.argv[9] if len(sys.argv) > 9 else "all"

        _html_pages = {k: Path(v) for k, v in _html_map.items()}
        _seg_type   = _seg.get("type", "overview")
        _duration   = max(_seg.get("duration", 8.0), 4.0)
        _out_mp4    = _job_dir / "segments" / f"seg_{_idx:02d}_raw.mp4"
        _out_mp4.parent.mkdir(parents=True, exist_ok=True)

        _headline   = _script.get("headline", "")
        _result     = None

        try:
            if _strategy == "article":
                _url = _seg.get("url", "")
                if _url:
                    from playwright_scraper import record_with_script, playwright_scroll_video
                    _mini = {"key_facts": [], "narration": _seg.get("text",""), "headline": _seg.get("text","")[:80]}
                    try:
                        record_with_script(_url, _out_mp4, _mini, _duration)
                    except Exception:
                        playwright_scroll_video(_url, _out_mp4, duration=_duration)
                    if _out_mp4.exists() and _out_mp4.stat().st_size > 20_000:
                        _result = _out_mp4

            elif _strategy == "infographic":
                _vkey = _seg.get("visual", "overview_infographic")
                _hp = _html_pages.get(_vkey)
                if _hp and _hp.exists():
                    from playwright_scraper import record_html_page
                    record_html_page(_hp, _out_mp4, duration=_duration)
                    if _out_mp4.exists() and _out_mp4.stat().st_size > 10_000:
                        _result = _out_mp4

            elif _strategy == "youtube":
                from youtube_search import find_broll_for_segment
                _broll = find_broll_for_segment(_seg, _job_id, _idx,
                                                headline=_headline, duration=_duration)
                if _broll and _broll.exists():
                    import shutil as _sh
                    _sh.copy2(str(_broll), str(_out_mp4))
                    if _out_mp4.stat().st_size > 20_000:
                        _result = _out_mp4

            elif _strategy == "avatar":
                from avatar_page import record_avatar
                _result = record_avatar(_seg, _script, _out_mp4,
                                        duration=_duration, job_id=_job_id)

            elif _strategy == "all":
                # Legacy: run full cascade (not recommended, kept for compatibility)
                _result = _record_segment(_seg, _idx, _html_pages, _job_dir,
                                          script=_script, job_id=_job_id)

            try:
                _result_f.write_text(json.dumps({"mp4": str(_result) if _result else None}))
            except Exception:
                pass
            _os._exit(0 if _result else 1)

        except BaseException as _e:
            try:
                _result_f.write_text(json.dumps({"mp4": None, "error": str(_e)}))
            except Exception:
                pass
            _os._exit(2)

    # ── Normal CLI ────────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="NewscastAI Category Pipeline")
    parser.add_argument("--category", "-c", default=None,
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
                        help="Resume or reference a specific job ID")
    parser.add_argument("--resume", action="store_true",
                        help="Resume the latest incomplete job (optionally filtered by --category)")
    parser.add_argument("--list-jobs", action="store_true",
                        help="List all jobs with their status and exit")

    args = parser.parse_args()

    # ── list-jobs ──────────────────────────────────────────────────────────────
    if args.list_jobs:
        jobs = list_jobs()
        if not jobs:
            print("No jobs found.")
        else:
            print(f"{'JOB ID':<30} {'CATEGORY':<20} {'STAGES DONE':<35} {'OUTPUT'}")
            print("-" * 100)
            for j in jobs:
                stages = ",".join(j["stages_done"]) or "-"
                output = "✓ " + Path(j["output"]).name if j["output"] else "incomplete"
                print(f"{j['job_id']:<30} {j['category']:<20} {stages:<35} {output}")
        sys.exit(0)

    # ── resume ─────────────────────────────────────────────────────────────────
    job_id = args.job_id
    if args.resume:
        job_id = find_latest_incomplete_job(args.category)
        if job_id is None:
            print(f"No incomplete job found{' for category: ' + args.category if args.category else ''}.")
            sys.exit(1)
        print(f"[resume] Resuming job: {job_id}")
        # Load args from checkpoint
        job_dir = TEMP_DIR / job_id
        ckpt = _load_checkpoint(job_dir)
        saved = ckpt.get("_args", {})
        if not args.category:
            args.category = saved.get("category", "news")
        if not args.countries and saved.get("countries"):
            args.countries = ",".join(saved["countries"]) if isinstance(saved["countries"], list) else saved["countries"]
        if args.duration == 120 and saved.get("target_duration"):
            args.duration = saved["target_duration"]
        if args.voice == "male_us" and saved.get("voice_key"):
            args.voice = saved["voice_key"]

    if not args.category:
        parser.error("--category is required (or use --resume to continue the last job)")

    countries = args.countries.split(",") if args.countries else None

    result = run_category_pipeline(
        category=args.category,
        countries=countries,
        max_per_country=args.max_per_country,
        voice_key=args.voice,
        target_duration=args.duration,
        output_filename=args.output,
        job_id=job_id,
        llm_provider=args.provider,
    )

    print("\nPipeline Summary:")
    for stage, info in result["stages"].items():
        status = info.get("status", "?")
        print(f"  {stage}: {status}")
        if status == "error":
            print(f"    Error: {info.get('error', '')}")

    output = result.get("output_video", "")
    if output:
        print(f"\nOutput: {output}")

    if result.get("output_video"):
        print(f"\nOutput: {result['output_video']}")
    else:
        print("\nPipeline completed with errors — no output video.")
        sys.exit(1)
