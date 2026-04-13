"""
pipeline.py — End-to-end orchestrator
Runs: scrape → extract → summarize → narrate → compose
Can be called directly or via the MCP server / agents.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).parent))

from scraper import fetch_article, scrape_text_only
from extractor import extract_videos_from_page
from summarizer import generate_full_script
from narrator import generate_all_narrations, get_audio_duration
from composer import compose_newscast
from playwright_scraper import record_with_script, playwright_scroll_video


def run_pipeline(
    url: str,
    duration_seconds: int = 90,
    voice_key: str = "male_us",
    output_filename: str = None,
    job_id: str = None,
    llm_provider: str = None,  # "claude" | "gemini" | None (auto-detect)
) -> dict:
    """
    Full newscast generation pipeline.

    Args:
        url: News article URL
        duration_seconds: Target video duration (60-180 recommended)
        voice_key: TTS voice (male_us, female_us, male_uk, female_uk, male_au, female_au)
        output_filename: Optional output filename (auto-generated if None)
        job_id: Optional job ID for temp file organization

    Returns:
        dict with output_video path, script, and metadata
    """
    if job_id is None:
        job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    print(f"\n{'='*60}")
    print(f"NewscastAI Pipeline — Job: {job_id}")
    print(f"URL: {url}")
    print(f"{'='*60}\n")

    results = {"job_id": job_id, "url": url, "stages": {}}

    job_dir = Path(__file__).parent.parent / "temp" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1: Scrape text + metadata (no video yet)
    print("Stage 1/5: Scraping article...")
    try:
        scrape_data = scrape_text_only(url, job_id)
        results["stages"]["scrape"] = {"status": "ok", "title": scrape_data["title"]}
        print(f"  Title: {scrape_data['title']}")
        print(f"  Text length: {len(scrape_data.get('text', ''))} chars")
    except Exception as e:
        results["stages"]["scrape"] = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")
        scrape_data = {"title": url, "text": "", "images": [], "job_dir": str(job_dir)}

    # Stage 2: Extract embedded videos
    print("\nStage 2/5: Extracting embedded videos...")
    try:
        article_html = scrape_data.get("html", "")
        if not article_html:
            raw = fetch_article(url)
            article_html = raw.get("html", "")
        extracted_videos = extract_videos_from_page(url, article_html, job_id)
        successful = [v for v in extracted_videos if v.get("success")]
        results["stages"]["extract"] = {"status": "ok", "videos_found": len(extracted_videos),
                                         "videos_downloaded": len(successful)}
        print(f"  Found: {len(extracted_videos)}, Downloaded: {len(successful)}")
    except Exception as e:
        results["stages"]["extract"] = {"status": "error", "error": str(e)}
        print(f"  WARNING: {e}")
        extracted_videos = []

    # Stage 3: Generate script
    print("\nStage 3/5: Generating newscast script...")
    try:
        script = generate_full_script(scrape_data, duration_seconds, provider=llm_provider)
        results["stages"]["script"] = {"status": "ok", "headline": script.get("headline", "")}
        print(f"  Headline: {script.get('headline', '')}")
        print(f"  Narration words: {len(script.get('narration', '').split())}")
    except Exception as e:
        results["stages"]["script"] = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")
        script = {
            "anchor_intro": "Here is our next story.",
            "headline": scrape_data.get("title", url),
            "narration": scrape_data.get("text", "No content available.")[:500],
            "key_facts": [],
            "closing_line": "Reporting for NewscastAI.",
            "lower_third_title": "Breaking News",
            "lower_third_name": "NewscastAI Reporter",
            "estimated_duration_sec": duration_seconds,
            "summary": "",
            "source_title": scrape_data.get("title", ""),
        }

    # Stage 4: Generate narration audio FIRST
    print("\nStage 4/5: Generating narration audio...")
    try:
        audio_sections = generate_all_narrations(script, job_id, voice_key)
        results["stages"]["audio"] = {"status": "ok", "sections": list(audio_sections.keys())}
        print(f"  Generated sections: {list(audio_sections.keys())}")
    except Exception as e:
        results["stages"]["audio"] = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")
        audio_sections = {}

    # Stage 4.5: Record Playwright video synced to narration audio duration
    print("\nStage 4.5: Recording browser video (synced to narration)...")
    scroll_video = None
    try:
        narration_audio = audio_sections.get("narration")
        scroll_mp4 = job_dir / "scroll.mp4"

        if narration_audio and Path(narration_audio).exists():
            audio_dur = get_audio_duration(Path(narration_audio))
            print(f"  Narration duration: {audio_dur:.1f}s — recording synced video")
            record_with_script(url, scroll_mp4, script, audio_dur)
        else:
            print("  No narration audio — recording 30s simple scroll")
            playwright_scroll_video(url, scroll_mp4, duration=30)

        if scroll_mp4.exists():
            scroll_video = str(scroll_mp4)
            print(f"  Scroll video: {scroll_video}")
    except Exception as e:
        print(f"  WARNING: Playwright recording failed: {e}")
        scroll_video = None

    # Stage 5: Compose final video
    print("\nStage 5/5: Composing final video...")
    try:
        final_video = compose_newscast(
            job_id=job_id,
            script=script,
            scroll_video=scroll_video,
            extracted_videos=extracted_videos,
            audio_sections=audio_sections,
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

    results["script"] = script
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <url> [duration_seconds] [voice_key]")
        print("Example: python pipeline.py https://bbc.com/news/... 90 female_us")
        sys.exit(1)

    url = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    voice = sys.argv[3] if len(sys.argv) > 3 else "male_us"

    result = run_pipeline(url, duration_seconds=duration, voice_key=voice)

    # Print summary
    print("\nPipeline Summary:")
    for stage, info in result["stages"].items():
        status = info.get("status", "?")
        print(f"  {stage}: {status}")
        if status == "error":
            print(f"    Error: {info.get('error', '')}")

    if result.get("output_video"):
        print(f"\nOutput: {result['output_video']}")
