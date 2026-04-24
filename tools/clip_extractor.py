"""
clip_extractor.py — Extract video clips by timestamp using yt-dlp.

Given video highlights with timestamps from NotebookLM:
  1. Download the exact timestamp range from the source video
  2. Save as a standalone MP4 clip
  3. Optionally re-upload to NotebookLM as a source
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

YT_DLP = "yt-dlp"


def _check_yt_dlp() -> bool:
    """Check if yt-dlp is installed."""
    try:
        r = subprocess.run([YT_DLP, "--version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _timestamp_to_seconds(ts) -> float:
    """Convert various timestamp formats to seconds."""
    if isinstance(ts, (int, float)):
        return float(ts)

    ts = str(ts).strip()

    # HH:MM:SS.mmm
    if ":" in ts:
        parts = ts.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)

    return float(ts)


def extract_clip(
    video_url: str,
    start_time,
    end_time,
    output_path: Path,
    quality: str = "best",
) -> dict:
    """
    Extract a clip from a video URL using yt-dlp --download-sections.

    Args:
        video_url: Source video URL (YouTube, Bilibili, etc.)
        start_time: Start time in seconds or "HH:MM:SS" format
        end_time: End time in seconds or "HH:MM:SS" format
        output_path: Where to save the output MP4
        quality: Quality preset ("best", "720p", "480p", "360p")

    Returns:
        {"success": bool, "path": str, "duration": float}
    """
    if not _check_yt_dlp():
        return {"success": False, "error": "yt-dlp not found"}

    output_path.parent.mkdir(parents=True, exist_ok=True)

    start_sec = _timestamp_to_seconds(start_time)
    end_sec = _timestamp_to_seconds(end_time)
    duration = end_sec - start_sec

    if duration <= 0:
        return {"success": False, "error": f"Invalid duration: {start_time} → {end_time}"}

    # Build format string
    format_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best",
        "360p": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best",
    }
    fmt = format_map.get(quality, format_map["720p"])

    # yt-dlp section format: *start-end
    section = f"*{start_sec:.1f}-{end_sec:.1f}"

    cmd = [
        YT_DLP,
        video_url,
        "--download-sections", section,
        "--format", fmt,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--output", str(output_path),
        "--force-keyframes-at-cuts",
        "--retries", "3",
        "--fragment-retries", "3",
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        if output_path.exists() and output_path.stat().st_size > 50_000:
            return {"success": True, "path": str(output_path), "duration": duration}

        # Fallback: download full video and trim with ffmpeg
        print(f"  [clip] Section download failed, trying full download + ffmpeg trim for {video_url}")
        return _extract_clip_fallback(video_url, start_sec, end_sec, output_path, fmt)

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Download timeout", "path": None}
    except Exception as e:
        return {"success": False, "error": str(e), "path": None}


def _extract_clip_fallback(
    video_url: str,
    start_sec: float,
    end_sec: float,
    output_path: Path,
    fmt: str,
) -> dict:
    """Fallback: download full video then trim with ffmpeg."""
    temp_full = output_path.with_name(output_path.stem + "_full" + output_path.suffix)

    try:
        cmd_dl = [
            YT_DLP,
            video_url,
            "--format", fmt,
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            "--output", str(temp_full),
            "--retries", "3",
        ]
        r = subprocess.run(cmd_dl, capture_output=True, text=True, timeout=240)

        if not temp_full.exists() or temp_full.stat().st_size < 50_000:
            return {"success": False, "error": "Full download failed"}

        # Trim with ffmpeg
        duration = end_sec - start_sec
        cmd_ffmpeg = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", str(temp_full),
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output_path),
        ]
        r2 = subprocess.run(cmd_ffmpeg, capture_output=True, text=True, timeout=60)

        # Clean up temp
        if temp_full.exists():
            temp_full.unlink()

        if output_path.exists() and output_path.stat().st_size > 50_000:
            return {"success": True, "path": str(output_path), "duration": duration}

        return {"success": False, "error": "FFmpeg trim failed"}

    except Exception as e:
        if temp_full.exists():
            temp_full.unlink()
        return {"success": False, "error": str(e)}


def extract_clips_batch(
    video_highlights: list[dict],
    output_dir: Path,
    quality: str = "720p",
) -> list[dict]:
    """
    Extract clips for a batch of video highlights.

    Args:
        video_highlights: List of {video_url, caption, timestamp_start, timestamp_end, duration}
        output_dir: Directory to save clips
        quality: Quality preset

    Returns:
        List of clip extraction results
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for i, vh in enumerate(video_highlights):
        url = vh.get("video_url", "")
        start = vh.get("timestamp_start", 0)
        end = vh.get("timestamp_end", "")
        caption = vh.get("caption", "")

        if not url:
            print(f"  [clip] [{i+1}] Skipping: no URL")
            continue

        # If end is relative duration, compute absolute
        if not end:
            duration = vh.get("duration", 30)
            if isinstance(start, (int, float)):
                end = start + duration
            else:
                start_sec = _timestamp_to_seconds(start)
                end = start_sec + duration

        output_path = output_dir / f"highlighted_caption_{i:03d}.mp4"

        print(f"  [clip] [{i+1}/{len(video_highlights)}] Extracting: {caption[:50]}... ({start} → {end})")

        try:
            result = extract_clip(url, start, end, output_path, quality)
            result["highlight_index"] = i
            result["caption"] = caption
            result["source_url"] = url
            results.append(result)
        except Exception as e:
            print(f"  [clip] Error on clip {i}: {e}")
            results.append({
                "success": False,
                "highlight_index": i,
                "caption": caption,
                "source_url": url,
                "error": str(e),
            })

        time.sleep(1)  # Delay between downloads

    successful = [r for r in results if r.get("success")]
    print(f"  [clip] Extracted {len(successful)}/{len(video_highlights)} clips")
    return results


def get_clip_file_paths(results: list[dict]) -> list[Path]:
    """Extract file paths from successful clip results."""
    return [Path(r["path"]) for r in results if r.get("success") and r.get("path")]


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("Usage: python clip_extractor.py <url> <start_time> <end_time> [output.mp4]")
        print("  Times can be seconds (number) or HH:MM:SS format")
        sys.exit(1)

    url = sys.argv[1]
    start = sys.argv[2]
    end = sys.argv[3]
    output = sys.argv[4] if len(sys.argv) > 4 else "clip_output.mp4"

    result = extract_clip(url, start, end, Path(output))
    print(json.dumps(result, indent=2))
