"""
category_pipeline_v2.py — NotebookLM-powered newscast pipeline orchestrator.

Pipeline:
  Title → Web Search (multi-country) → Video Search (multi-platform)
  → NotebookLM (source ingest) → Highlights → Playwright Highlight Videos
  → yt-dlp Clip Extraction → Infographic → Narrative (>30 min)
  → NotebookLM Video Generation → Final MP4

Supports checkpoint/resume from any failed stage.

Usage:
    python3 category_pipeline_v2.py --title "US-China Trade War 2025"
        [--countries us,uk,cn,jp] [--duration 1800] [--output out.mp4]
        [--resume JOB_ID] [--skip-stages 1,2]
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
from datetime import datetime

# Disable ONNX runtime thread affinity
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))

from search_web import search_web, ALL_COUNTRY_CODES
from search_video import search_video, get_video_urls
from notebooklm_integration import (
    check_auth, create_notebook, list_notebooks, add_source, add_sources_batch,
    wait_for_sources, list_sources, ask, ask_for_structured,
    generate_audio, generate_video, generate_infographic, generate_report,
    list_artifacts, wait_for_artifact,
    download_audio, download_video, download_infographic, download_report,
    deep_research,
)
from highlight_recorder import record_highlights
from clip_extractor import extract_clips_batch, get_clip_file_paths

# ── Paths ─────────────────────────────────────────────────────────────────────

TEMP_DIR   = Path(__file__).parent.parent / "temp"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def _checkpoint_path(job_id: str) -> Path:
    return TEMP_DIR / job_id / "_checkpoint_v2.json"


def _save_checkpoint(job_id: str, data: dict):
    """Save pipeline checkpoint."""
    cp = _load_checkpoint(job_id) or {}
    cp.update(data)
    cp["_args"] = cp.get("_args", {})
    cp["_updated"] = datetime.utcnow().isoformat()
    path = _checkpoint_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cp, indent=2, default=str))


def _load_checkpoint(job_id: str) -> dict | None:
    """Load pipeline checkpoint."""
    path = _checkpoint_path(job_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    return None


def _stage_done(ckpt: dict, stage: str) -> bool:
    return ckpt.get(f"stage_{stage}_done", False)


def _mark_stage(ckpt: dict, stage: str, result: dict = None):
    ckpt[f"stage_{stage}_done"] = True
    if result:
        ckpt[f"stage_{stage}_result"] = result


# ── Stage implementations ────────────────────────────────────────────────────

def _stage1_web_search(title: str, countries: list[str], max_per_country: int) -> dict:
    """Stage 1: Multi-country web search."""
    print("\n" + "=" * 60)
    print("Stage 1/10: Multi-Country Web Search")
    print("=" * 60)
    return search_web(title, countries=countries, max_per_country=max_per_country)


def _stage2_video_search(title: str) -> dict:
    """Stage 2: Multi-platform video search."""
    print("\n" + "=" * 60)
    print("Stage 2/10: Multi-Platform Video Search")
    print("=" * 60)
    return search_video(title)


def _stage3_notebooklm_ingest(
    title: str,
    article_urls: list[str],
    video_urls: list[str],
    existing_notebook_id: str = None,
) -> dict:
    """Stage 3: Create NotebookLM notebook and ingest sources via deep research."""
    print("\n" + "=" * 60)
    print("Stage 3/10: NotebookLM Source Ingestion")
    print("=" * 60)

    # Use existing notebook if provided
    if existing_notebook_id:
        notebook_id = existing_notebook_id
        print(f"  [nblm] Using existing notebook: {notebook_id}")
        # Count existing sources
        from notebooklm_integration import list_sources
        existing = list_sources(notebook_id)
        print(f"  [nblm] Existing sources: {len(existing)}")
        if len(existing) > 0:
            return {
                "notebook_id": notebook_id,
                "source_ids": [s["id"] for s in existing],
                "total_sources": len(existing),
                "article_count": len(article_urls),
                "video_count": len(video_urls),
                "research_sources": len(existing),
                "video_source_count": 0,
            }
    else:
        # Create notebook
        nb = create_notebook(f"Newscast: {title}")
        notebook_id = nb["id"]
        print(f"  [nblm] Created notebook: {notebook_id}")

    # Use deep research for comprehensive source collection
    # (Individual URL additions are unreliable; deep research finds and imports sources automatically)
    print(f"  [nblm] Running deep research for: {title}")
    print(f"  [nblm] Note: {len(video_urls)} video URLs found but using deep research for reliable ingestion")
    n_research = deep_research(notebook_id, title, timeout=300)

    print(f"  [nblm] Total sources after ingestion: {n_research}")

    return {
        "notebook_id": notebook_id,
        "source_ids": [],
        "total_sources": n_research,
        "article_count": len(article_urls),
        "video_count": len(video_urls),
        "research_sources": n_research,
        "video_source_count": 0,
    }


def _stage4_generate_highlights(notebook_id: str) -> dict:
    """Stage 4: Ask NotebookLM to generate highlights from web article sources."""
    print("\n" + "=" * 60)
    print("Stage 4/10: Generating Highlights from Pages")
    print("=" * 60)

    prompt = (
        "Analyze all web article sources (URLs, not videos). "
        "Identify the key highlights and important text passages. "
        "For each highlight, provide: the source page URL, the exact highlight text (2-4 sentences), "
        "and brief context about why it matters. "
        "Return as a JSON array with objects: {page_url, highlight_text, context}. "
        "Aim for 10-20 high-quality highlights that cover the main story comprehensively."
    )

    result = ask_for_structured(notebook_id, prompt, "json")
    answer = result.get("answer") or ""
    error = result.get("error")

    if error and not answer:
        print(f"  [nblm] Ask failed: {error[:200]}")
        return {"highlights": [], "count": 0, "error": error[:500]}

    # Parse JSON from answer
    try:
        # Strip markdown code fences if present
        cleaned = answer.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("\n", 1)[0]
        cleaned = cleaned.strip()

        highlights = json.loads(cleaned)
        if isinstance(highlights, list):
            print(f"  [nblm] Parsed {len(highlights)} highlights")
            return {"highlights": highlights, "count": len(highlights)}
    except json.JSONDecodeError:
        print(f"  [nblm] Failed to parse highlights JSON. Raw answer: {answer[:200]}")

    return {"highlights": [], "count": 0, "raw_answer": answer[:500]}


def _stage5_record_highlights(highlights: list[dict], job_dir: Path) -> dict:
    """Stage 5: Record Playwright highlight videos and upload to NotebookLM."""
    print("\n" + "=" * 60)
    print("Stage 5/10: Recording Highlight Videos")
    print("=" * 60)

    if not highlights:
        print("  [highlight] No highlights to record")
        return {"highlight_videos": [], "new_source_ids": []}

    highlight_dir = job_dir / "highlight_videos"
    results = record_highlights(highlights, highlight_dir, duration_per_highlight=8.0)

    successful_paths = [r["path"] for r in results if r.get("success") and r.get("path")]
    print(f"  [highlight] {len(successful_paths)} highlight videos recorded")

    return {
        "highlight_videos": successful_paths,
        "highlight_results": results,
        "count": len(successful_paths),
    }


def _stage5b_upload_highlight_videos(notebook_id: str, highlight_videos: list[str]) -> list[str]:
    """Upload recorded highlight videos back to NotebookLM as sources."""
    print(f"  [nblm] Uploading {len(highlight_videos)} highlight videos as sources...")
    new_source_ids = []
    for path in highlight_videos:
        if Path(path).exists():
            result = add_source(notebook_id, str(path))
            if result.get("source_id"):
                new_source_ids.append(result["source_id"])
                print(f"    Uploaded: {Path(path).name}")
    return new_source_ids


def _stage6_extract_video_highlights(notebook_id: str) -> dict:
    """Stage 6: Ask NotebookLM for video highlights with timestamps."""
    print("\n" + "=" * 60)
    print("Stage 6/10: Extracting Video Highlights with Timestamps")
    print("=" * 60)

    prompt = (
        "Analyze all video sources. For each video, identify the most important segments. "
        "For each segment, provide: the video URL, a caption/summary of what's shown, "
        "the start timestamp, end timestamp, and duration in seconds. "
        "Return as a JSON array with objects: "
        "{video_url, caption, timestamp_start (seconds), timestamp_end (seconds), duration (seconds)}. "
        "Aim for 5-15 high-quality segments across all videos."
    )

    result = ask_for_structured(notebook_id, prompt, "json")
    answer = result.get("answer", "")

    try:
        cleaned = answer.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("\n", 1)[0]
        cleaned = cleaned.strip()

        video_highlights = json.loads(cleaned)
        if isinstance(video_highlights, list):
            print(f"  [nblm] Parsed {len(video_highlights)} video highlights")
            return {"video_highlights": video_highlights, "count": len(video_highlights)}
    except json.JSONDecodeError:
        print(f"  [nblm] Failed to parse video highlights JSON. Raw answer: {answer[:200]}")

    return {"video_highlights": [], "count": 0}


def _stage7_extract_clips(video_highlights: list[dict], job_dir: Path) -> dict:
    """Stage 7: Use yt-dlp to extract clips by timestamp."""
    print("\n" + "=" * 60)
    print("Stage 7/10: Extracting Video Clips by Timestamp")
    print("=" * 60)

    if not video_highlights:
        print("  [clip] No video highlights to extract")
        return {"clips": [], "clip_paths": []}

    clip_dir = job_dir / "clips"
    results = extract_clips_batch(video_highlights, clip_dir, quality="720p")

    clip_paths = [r["path"] for r in results if r.get("success") and r.get("path")]
    print(f"  [clip] {len(clip_paths)} clips extracted")

    return {"clips": clip_paths, "clip_results": results, "count": len(clip_paths)}


def _stage7b_upload_clips(notebook_id: str, clip_paths: list[str]) -> list[str]:
    """Upload extracted clips back to NotebookLM as sources."""
    print(f"  [nblm] Uploading {len(clip_paths)} clips as sources...")
    new_source_ids = []
    for path in clip_paths:
        if Path(path).exists():
            result = add_source(notebook_id, str(path))
            if result.get("source_id"):
                new_source_ids.append(result["source_id"])
    return new_source_ids


def _stage8_generate_infographic(notebook_id: str, job_dir: Path) -> dict:
    """Stage 8: Ask NotebookLM to generate infographics."""
    print("\n" + "=" * 60)
    print("Stage 8/10: Generating Infographics")
    print("=" * 60)

    artifact = generate_infographic(
        notebook_id,
        instructions=(
            "Create comprehensive infographics covering: key statistics, "
            "country-by-country comparisons, timeline of events, cause and effect relationships. "
            "Include data visualizations, maps, and charts where relevant."
        ),
        orientation="landscape",
        detail="detailed",
        style="professional",
    )

    artifact_id = artifact.get("task_id", "")
    if not artifact_id:
        print("  [nblm] Infographic generation failed to start")
        return {"success": False}

    print(f"  [nblm] Infographic generation started: {artifact_id}")
    print(f"  [nblm] Waiting for completion (up to 20 min)...")

    status = wait_for_artifact(notebook_id, artifact_id, timeout=1200)

    if status == "completed":
        output_path = job_dir / "infographic.png"
        ok = download_infographic(notebook_id, artifact_id, str(output_path))
        return {
            "success": ok,
            "artifact_id": artifact_id,
            "path": str(output_path) if ok else None,
        }

    return {"success": False, "artifact_id": artifact_id, "status": status}


def _stage9_generate_narrative(notebook_id: str, job_dir: Path, min_duration_minutes: int = 30) -> dict:
    """Stage 9: Generate long-form narrative audio (>30 min) and transcript."""
    print("\n" + "=" * 60)
    print(f"Stage 9/10: Generating Long-Form Narrative (>{min_duration_minutes} min)")
    print("=" * 60)

    # Generate audio
    artifact = generate_audio(
        notebook_id,
        instructions=(
            f"Create a comprehensive deep-dive podcast covering ALL topics and sources. "
            f"Target duration: at least {min_duration_minutes} minutes of detailed narration. "
            f"Cover every angle: background, context, expert opinions, data points, "
            f"comparisons, implications, and future outlook. Be thorough and analytical."
        ),
        format_type="deep-dive",
        length="long",
    )

    artifact_id = artifact.get("task_id", "")
    if not artifact_id:
        print("  [nblm] Audio generation failed to start")
        return {"success": False}

    print(f"  [nblm] Audio generation started: {artifact_id}")
    print(f"  [nblm] Waiting for completion (up to 30 min)...")

    status = wait_for_artifact(notebook_id, artifact_id, timeout=1800)

    audio_path = None
    transcript = None

    if status == "completed":
        output_path = job_dir / "narrative.mp3"
        ok = download_audio(notebook_id, artifact_id, str(output_path))
        if ok:
            audio_path = str(output_path)
            print(f"  [nblm] Audio downloaded: {audio_path}")

        # Get transcript
        print(f"  [nblm] Fetching transcript...")
        transcript_result = ask(notebook_id, "Provide the full transcript of the audio overview you just generated.")
        transcript = transcript_result.get("answer", "")

    return {
        "success": audio_path is not None,
        "artifact_id": artifact_id,
        "audio_path": audio_path,
        "transcript": transcript[:5000] if transcript else None,
        "transcript_full": transcript,
        "status": status,
    }


def _stage10_generate_overview_video(notebook_id: str, job_dir: Path, title: str) -> dict:
    """Stage 10: Generate NotebookLM video overview based on all collected sources."""
    print("\n" + "=" * 60)
    print("Stage 10/10: Generating Video Overview")
    print("=" * 60)

    artifact = generate_video(
        notebook_id,
        instructions=(
            f"Create a comprehensive video explainer for the topic: {title}. "
            f"Use ALL available sources including article highlights, video clips, "
            f"infographics, and data. Create a well-structured visual narrative "
            f"that covers the full story with engaging visuals, data visualizations, "
            f"and clear explanations."
        ),
        format_type="explainer",
        style="auto",
    )

    artifact_id = artifact.get("task_id", "")
    if not artifact_id:
        print("  [nblm] Video generation failed to start")
        return {"success": False}

    print(f"  [nblm] Video generation started: {artifact_id}")
    print(f"  [nblm] Waiting for completion (up to 45 min)...")

    status = wait_for_artifact(notebook_id, artifact_id, timeout=2700)

    if status == "completed":
        output_path = job_dir / "overview.mp4"
        ok = download_video(notebook_id, artifact_id, str(output_path))
        return {
            "success": ok,
            "artifact_id": artifact_id,
            "path": str(output_path) if ok else None,
        }

    return {"success": False, "artifact_id": artifact_id, "status": status}


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_category_pipeline_v2(
    title: str,
    countries: list[str] = None,
    max_per_country: int = 15,
    output_filename: str = None,
    job_id: str = None,
    resume_from: str = None,
    skip_stages: list[int] = None,
    notebook_id: str = None,
) -> dict:
    """
    Run the full NotebookLM-powered newscast pipeline.

    Args:
        title: Topic / title for the newscast
        countries: Country codes to search
        max_per_country: Max articles per country
        output_filename: Output MP4 filename
        job_id: Job ID (auto-generated if None)
        resume_from: Resume from a specific job_id checkpoint
        skip_stages: List of stage numbers to skip

    Returns:
        dict with output paths, metadata, and stage results
    """
    if job_id is None:
        job_id = f"v2_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    if countries is None:
        countries = ALL_COUNTRY_CODES

    if skip_stages is None:
        skip_stages = []

    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Load checkpoint if resuming
    ckpt = _load_checkpoint(job_id) if resume_from else None
    if ckpt is None:
        ckpt = {
            "pipeline_version": "v2",
            "title": title,
            "countries": countries,
            "_args": {
                "title": title,
                "countries": countries,
                "max_per_country": max_per_country,
            },
            "_started": datetime.utcnow().isoformat(),
        }

    print(f"\n{'#'*60}")
    print(f"# NewscastAI Pipeline v2 — Job: {job_id}")
    print(f"# Topic: {title}")
    print(f"# Countries: {', '.join(countries)}")
    print(f"# {'#'*60}\n")

    # Check auth first
    auth = check_auth()
    if not auth.get("authenticated"):
        print("ERROR: NotebookLM authentication required.")
        print("Run: notebooklm login")
        return {"error": "Not authenticated", "job_id": job_id}

    results = {"job_id": job_id, "title": title, "stages": {}}

    # If using existing notebook and Stage 3 is skipped, set notebook_id in checkpoint
    if notebook_id and 3 in skip_stages:
        ckpt["notebook_id"] = notebook_id

    # ── Stage 1: Web Search ──────────────────────────────────────────────
    if 1 not in skip_stages and not _stage_done(ckpt, "1"):
        search_result = _stage1_web_search(title, countries, max_per_country)
        ckpt["article_urls"] = [a["url"] for a in search_result.get("articles", [])]
        _mark_stage(ckpt, "1", {"total_articles": search_result.get("total_found", 0)})
        _save_checkpoint(job_id, ckpt)
    results["stages"]["1_web_search"] = ckpt.get("stage_1_result", {})

    # ── Stage 2: Video Search ────────────────────────────────────────────
    if 2 not in skip_stages and not _stage_done(ckpt, "2"):
        video_result = _stage2_video_search(title)
        ckpt["video_urls"] = get_video_urls(video_result, max_urls=50)
        _mark_stage(ckpt, "2", {"total_videos": video_result.get("total_found", 0)})
        _save_checkpoint(job_id, ckpt)
    results["stages"]["2_video_search"] = ckpt.get("stage_2_result", {})

    # ── Stage 3: NotebookLM Ingestion ────────────────────────────────────
    if 3 not in skip_stages and not _stage_done(ckpt, "3"):
        ingest_result = _stage3_notebooklm_ingest(
            title,
            ckpt.get("article_urls", []),
            ckpt.get("video_urls", []),
            notebook_id,
        )
        ckpt["notebook_id"] = ingest_result["notebook_id"]
        ckpt["source_ids"] = ingest_result["source_ids"]
        _mark_stage(ckpt, "3", ingest_result)
        _save_checkpoint(job_id, ckpt)
    results["stages"]["3_ingestion"] = ckpt.get("stage_3_result", {})

    # ── Stage 4: Generate Highlights ─────────────────────────────────────
    if 4 not in skip_stages and not _stage_done(ckpt, "4"):
        highlights_result = _stage4_generate_highlights(ckpt["notebook_id"])
        ckpt["highlights"] = highlights_result.get("highlights", [])
        _mark_stage(ckpt, "4", highlights_result)
        _save_checkpoint(job_id, ckpt)
    results["stages"]["4_highlights"] = ckpt.get("stage_4_result", {})

    # ── Stage 5: Record Highlight Videos ─────────────────────────────────
    if 5 not in skip_stages and not _stage_done(ckpt, "5"):
        record_result = _stage5_record_highlights(ckpt.get("highlights", []), job_dir)
        ckpt["highlight_videos"] = record_result.get("highlight_videos", [])
        _mark_stage(ckpt, "5", record_result)
        _save_checkpoint(job_id, ckpt)

        # Stage 5b: Upload highlight videos to NotebookLM
        if ckpt.get("highlight_videos"):
            new_ids = _stage5b_upload_highlight_videos(
                ckpt["notebook_id"], ckpt["highlight_videos"]
            )
            ckpt["highlight_video_source_ids"] = new_ids
    results["stages"]["5_highlight_videos"] = ckpt.get("stage_5_result", {})

    # ── Stage 6: Extract Video Highlights ────────────────────────────────
    if 6 not in skip_stages and not _stage_done(ckpt, "6"):
        vh_result = _stage6_extract_video_highlights(ckpt["notebook_id"])
        ckpt["video_highlights"] = vh_result.get("video_highlights", [])
        _mark_stage(ckpt, "6", vh_result)
        _save_checkpoint(job_id, ckpt)
    results["stages"]["6_video_highlights"] = ckpt.get("stage_6_result", {})

    # ── Stage 7: Extract Clips ───────────────────────────────────────────
    if 7 not in skip_stages and not _stage_done(ckpt, "7"):
        clip_result = _stage7_extract_clips(ckpt.get("video_highlights", []), job_dir)
        ckpt["clip_paths"] = clip_result.get("clips", [])
        _mark_stage(ckpt, "7", clip_result)
        _save_checkpoint(job_id, ckpt)

        # Stage 7b: Upload clips to NotebookLM
        if ckpt.get("clip_paths"):
            new_ids = _stage7b_upload_clips(ckpt["notebook_id"], ckpt["clip_paths"])
            ckpt["clip_source_ids"] = new_ids
    results["stages"]["7_clips"] = ckpt.get("stage_7_result", {})

    # ── Stage 8: Generate Infographic ────────────────────────────────────
    if 8 not in skip_stages and not _stage_done(ckpt, "8"):
        infographic_result = _stage8_generate_infographic(ckpt["notebook_id"], job_dir)
        ckpt["infographic_path"] = infographic_result.get("path")
        _mark_stage(ckpt, "8", infographic_result)
        _save_checkpoint(job_id, ckpt)
    results["stages"]["8_infographic"] = ckpt.get("stage_8_result", {})

    # ── Stage 9: Generate Narrative ──────────────────────────────────────
    if 9 not in skip_stages and not _stage_done(ckpt, "9"):
        narrative_result = _stage9_generate_narrative(ckpt["notebook_id"], job_dir, min_duration_minutes=30)
        ckpt["narrative_audio"] = narrative_result.get("audio_path")
        ckpt["narrative_transcript"] = narrative_result.get("transcript")
        _mark_stage(ckpt, "9", narrative_result)
        _save_checkpoint(job_id, ckpt)
    results["stages"]["9_narrative"] = ckpt.get("stage_9_result", {})

    # ── Stage 10: Generate Overview Video ────────────────────────────────
    if 10 not in skip_stages and not _stage_done(ckpt, "10"):
        overview_result = _stage10_generate_overview_video(ckpt["notebook_id"], job_dir, title)
        ckpt["overview_video"] = overview_result.get("path")
        _mark_stage(ckpt, "10", overview_result)
        _save_checkpoint(job_id, ckpt)
    results["stages"]["10_overview"] = ckpt.get("stage_10_result", {})

    # ── Final: Copy to output ────────────────────────────────────────────
    final_output = None
    overview_path = ckpt.get("overview_video")
    if overview_path and Path(overview_path).exists():
        output_filename = output_filename or f"newscast_{job_id}.mp4"
        final = OUTPUT_DIR / output_filename
        shutil.copy2(overview_path, final)
        final_output = str(final)
        print(f"\n{'='*60}")
        print(f"SUCCESS! Output: {final_output}")
        print(f"{'='*60}\n")

    results["output_video"] = final_output
    results["job_dir"] = str(job_dir)
    results["notebook_id"] = ckpt.get("notebook_id")

    ckpt["_completed"] = datetime.utcnow().isoformat()
    _save_checkpoint(job_id, ckpt)

    return results


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NewscastAI v2 — NotebookLM-powered pipeline")
    parser.add_argument("--title", required=True, help="Topic/title for the newscast")
    parser.add_argument("--countries", default=None, help="Comma-separated country codes")
    parser.add_argument("--max-per-country", type=int, default=15, help="Max articles per country")
    parser.add_argument("--output", default=None, help="Output MP4 filename")
    parser.add_argument("--job-id", default=None, help="Job ID")
    parser.add_argument("--resume", default=None, help="Resume from job ID checkpoint")
    parser.add_argument("--notebook-id", default=None, help="Use existing notebook ID instead of creating new one")
    parser.add_argument("--skip-stages", default="", help="Comma-separated stage numbers to skip")
    args = parser.parse_args()

    countries = args.countries.split(",") if args.countries else None
    skip_stages = [int(x) for x in args.skip_stages.split(",") if x.strip().isdigit()] if args.skip_stages else []

    result = run_category_pipeline_v2(
        title=args.title,
        countries=countries,
        max_per_country=args.max_per_country,
        output_filename=args.output,
        job_id=args.job_id,
        resume_from=args.resume,
        skip_stages=skip_stages,
        notebook_id=args.notebook_id,
    )

    # Print summary
    print("\nPipeline Summary:")
    for stage, info in result.get("stages", {}).items():
        status = "ok" if info else "skipped"
        print(f"  {stage}: {status}")

    if result.get("output_video"):
        print(f"\nOutput: {result['output_video']}")
    else:
        print("\nNo output video produced (check stage results)")
