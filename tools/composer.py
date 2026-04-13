"""
composer.py — Final video composition
Combines: scroll video + extracted clips + narration audio + captions + lower thirds
Produces a broadcast-ready MP4.
"""

import json
import subprocess
import os
from pathlib import Path
from typing import Optional

OUTPUT_DIR = Path(__file__).parent.parent / "output"
ASSETS_DIR = Path(__file__).parent.parent / "assets"
TEMP_DIR = Path(__file__).parent.parent / "temp"


def get_video_duration(path: Path) -> float:
    """Get video duration in seconds."""
    result = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)
    ], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            d = stream.get("duration")
            if d:
                return float(d)
    except Exception:
        pass
    return 0.0


def add_lower_third(
    input_video: Path,
    output_video: Path,
    title: str,
    subtitle: str,
    start_sec: float = 1.0,
    duration_sec: float = 5.0,
) -> Path:
    """Burn a lower-third graphic into a video using ffmpeg drawtext (textfile= for safe escaping)."""
    output_video.parent.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # Write text to temp files — avoids all ffmpeg escaping issues (use resolved absolute paths)
    title_file = (TEMP_DIR / "lt_title.txt").resolve()
    subtitle_file = (TEMP_DIR / "lt_subtitle.txt").resolve()
    title_file.write_text(title[:60], encoding="utf-8")
    subtitle_file.write_text(subtitle[:50], encoding="utf-8")

    # For 1920x1080: use hardcoded pixel values (ih/iw expressions fail on this ffmpeg/Android build)
    # ih=1080: bar at y=960, title at y=990, subtitle at y=1030
    W, H = 1920, 1080
    bar_y = H - 120   # 960
    title_y = H - 90  # 990
    sub_y = H - 50    # 1030

    # Two-pass: drawbox then drawtext (combining them in one -vf fails on this build)
    pass1 = output_video.with_suffix(".p1.mp4")

    # Pass 1: draw background bar
    cmd1 = [
        "ffmpeg", "-y", "-i", str(input_video),
        "-vf", f"drawbox=x=0:y={bar_y}:w={W}:h=120:color=black:t=fill",
        "-c:v", "libx264", "-c:a", "copy", str(pass1)
    ]
    r1 = subprocess.run(cmd1, capture_output=True, text=True)
    if r1.returncode != 0:
        raise RuntimeError(f"lower-third pass1 error: {r1.stderr[-300:]}")

    # Pass 2: draw text overlays (two separate passes for title + subtitle)
    pass2 = output_video.with_suffix(".p2.mp4")
    cmd2 = [
        "ffmpeg", "-y", "-i", str(pass1),
        "-vf", f"drawtext=textfile='{title_file.as_posix()}':fontsize=36:fontcolor=white:x=20:y={title_y}",
        "-c:v", "libx264", "-c:a", "copy", str(pass2)
    ]
    r2 = subprocess.run(cmd2, capture_output=True, text=True)
    if r2.returncode != 0:
        pass1.unlink(missing_ok=True)
        raise RuntimeError(f"lower-third pass2 error: {r2.stderr[-300:]}")

    # Pass 3: add subtitle text
    cmd3 = [
        "ffmpeg", "-y", "-i", str(pass2),
        "-vf", f"drawtext=textfile='{subtitle_file.as_posix()}':fontsize=24:fontcolor=0xaaaaaa:x=20:y={sub_y}",
        "-c:v", "libx264", "-c:a", "copy", str(output_video)
    ]
    r3 = subprocess.run(cmd3, capture_output=True, text=True)
    pass1.unlink(missing_ok=True)
    pass2.unlink(missing_ok=True)
    if r3.returncode != 0:
        raise RuntimeError(f"lower-third pass3 error: {r3.stderr[-300:]}")
    return output_video


def add_news_ticker(input_video: Path, output_video: Path, headline: str) -> Path:
    """Add a scrolling news ticker at the bottom."""
    output_video.parent.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # Write ticker text to file to avoid escaping issues
    ticker_text = f"  {headline}  -  {headline}  -  {headline}  "
    ticker_file = TEMP_DIR.resolve() / "ticker.txt"
    ticker_file.write_text(ticker_text, encoding="utf-8")
    # Hardcoded for 1920x1080 (expressions like ih/iw fail on this Android ffmpeg build)
    W, H = 1920, 1080
    ticker_y = H - 30   # 1050
    text_y = H - 22     # 1058
    scroll_speed = 150  # pixels per second

    # Two passes: drawbox then drawtext
    pass1 = output_video.with_suffix(".tk1.mp4")
    cmd1 = [
        "ffmpeg", "-y", "-i", str(input_video),
        "-vf", f"drawbox=x=0:y={ticker_y}:w={W}:h=30:color=0xcc0000:t=fill",
        "-c:v", "libx264", "-c:a", "copy", str(pass1)
    ]
    r1 = subprocess.run(cmd1, capture_output=True, text=True)
    if r1.returncode != 0:
        raise RuntimeError(f"ticker pass1 error: {r1.stderr[-300:]}")

    cmd2 = [
        "ffmpeg", "-y", "-i", str(pass1),
        "-vf", f"drawtext=textfile='{ticker_file.as_posix()}':fontsize=18:fontcolor=white:x={W}-{scroll_speed}*t:y={text_y}",
        "-c:v", "libx264", "-c:a", "copy", str(output_video)
    ]
    r2 = subprocess.run(cmd2, capture_output=True, text=True)
    pass1.unlink(missing_ok=True)
    if r2.returncode != 0:
        raise RuntimeError(f"ticker pass2 error: {r2.stderr[-300:]}")
    return output_video


def _has_audio_stream(path: Path) -> bool:
    """Return True if the video file has an audio stream."""
    result = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)
    ], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        return any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    except Exception:
        return False


def _add_silent_audio(input_path: Path, output_path: Path) -> Path:
    """Add a silent AAC audio track to a video-only file."""
    dur = get_video_duration(input_path)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac",
        "-t", str(dur),
        str(output_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"add silent audio error: {r.stderr[-200:]}")
    return output_path


def concat_videos(video_paths: list[Path], output_path: Path) -> Path:
    """
    Concatenate multiple videos using ffmpeg concat demuxer.
    Normalises streams first: any video-only clip gets a silent audio track
    so that the concat doesn't drop audio from the whole output.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # Check if any clip has audio
    any_audio = any(_has_audio_stream(p) for p in video_paths)

    normalised = []
    for i, p in enumerate(video_paths):
        if any_audio and not _has_audio_stream(p):
            # Pad with silent audio so all clips have matching streams
            padded = TEMP_DIR / f"concat_pad_{i}.mp4"
            try:
                _add_silent_audio(p, padded)
                normalised.append(padded)
            except Exception as e:
                print(f"[concat] silent audio pad failed for {p.name}: {e}")
                normalised.append(p)
        else:
            normalised.append(p)

    concat_list = TEMP_DIR / "concat_final.txt"
    with open(concat_list, "w") as f:
        for p in normalised:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:v", "libx264", "-c:a", "aac",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"concat error: {result.stderr[-400:]}")
    return output_path


def merge_audio_video(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    """Replace or mix audio track in video with narration."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vid_dur = get_video_duration(video_path)

    # Mix: original audio at low volume + narration at full volume
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-filter_complex",
        "[0:a]volume=0.15[orig];[1:a]volume=1.0[narr];[orig][narr]amix=inputs=2:duration=first[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Fallback: just use narration audio, no original
        cmd2 = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac",
            "-shortest",
            str(output_path)
        ]
        result2 = subprocess.run(cmd2, capture_output=True, text=True)
        if result2.returncode != 0:
            raise RuntimeError(f"audio merge error: {result2.stderr[-400:]}")
    return output_path


def create_title_card(title: str, subtitle: str, duration_sec: float, output_path: Path) -> Path:
    """Create a black title card with text as intro."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # Use textfiles to avoid ffmpeg escaping issues (resolved absolute paths)
    title_file = (TEMP_DIR / "card_title.txt").resolve()
    subtitle_file = (TEMP_DIR / "card_subtitle.txt").resolve()
    title_file.write_text(title[:60], encoding="utf-8")
    subtitle_file.write_text(subtitle[:80], encoding="utf-8")

    # Hardcoded for 1920x1080 — centered x approximated, no expressions
    # Three passes: each drawtext separately (combining fails on Android ffmpeg)
    pass1 = output_path.with_suffix(".c1.mp4")
    pass2 = output_path.with_suffix(".c2.mp4")

    base_cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
                f"color=black:size=1920x1080:rate=25:duration={duration_sec}"]

    r1 = subprocess.run(base_cmd + [
        "-vf", "drawtext=text='BREAKING NEWS':fontsize=28:fontcolor=0xff0000:x=760:y=440",
        "-c:v", "libx264", str(pass1)
    ], capture_output=True, text=True)
    if r1.returncode != 0:
        raise RuntimeError(f"title card pass1 error: {r1.stderr[-200:]}")

    r2 = subprocess.run([
        "ffmpeg", "-y", "-i", str(pass1),
        "-vf", f"drawtext=textfile='{title_file.as_posix()}':fontsize=48:fontcolor=white:x=100:y=490",
        "-c:v", "libx264", "-c:a", "copy", str(pass2)
    ], capture_output=True, text=True)
    pass1.unlink(missing_ok=True)
    if r2.returncode != 0:
        raise RuntimeError(f"title card pass2 error: {r2.stderr[-200:]}")

    r3 = subprocess.run([
        "ffmpeg", "-y", "-i", str(pass2),
        "-vf", f"drawtext=textfile='{subtitle_file.as_posix()}':fontsize=28:fontcolor=0xaaaaaa:x=100:y=560",
        "-c:v", "libx264", "-c:a", "copy", str(output_path)
    ], capture_output=True, text=True)
    pass2.unlink(missing_ok=True)
    if r3.returncode != 0:
        raise RuntimeError(f"title card pass3 error: {r3.stderr[-200:]}")
    return output_path


def compose_newscast(
    job_id: str,
    script: dict,
    scroll_video: Optional[str],
    extracted_videos: list[dict],
    audio_sections: dict,
    output_filename: Optional[str] = None,
) -> Path:
    """
    Full composition pipeline.
    Returns path to final output MP4.
    """
    job_dir = TEMP_DIR / job_id
    output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    out_name = output_filename or f"newscast_{job_id}.mp4"
    final_output = output_dir / out_name

    segments = []

    # 1. Title card (3 seconds)
    title_card = job_dir / "title_card.mp4"
    print("[composer] Creating title card...")
    create_title_card(
        script.get("lower_third_title", script.get("source_title", "Breaking News")),
        script.get("headline", ""),
        3.0,
        title_card
    )
    segments.append(title_card)

    # 2. Main visual: scroll video or first extracted video
    main_visual = None
    if scroll_video and Path(scroll_video).exists():
        main_visual = Path(scroll_video)
        print(f"[composer] Using scroll video: {main_visual}")
    elif extracted_videos:
        for ev in extracted_videos:
            if ev.get("success") and ev.get("path"):
                main_visual = Path(ev["path"])
                print(f"[composer] Using extracted video: {main_visual}")
                break

    if main_visual and audio_sections.get("narration"):
        # Merge narration audio with main visual
        merged = job_dir / "main_with_audio.mp4"
        print("[composer] Merging narration audio with visual...")
        merge_audio_video(main_visual, Path(audio_sections["narration"]), merged)

        # Add lower third
        with_lower_third = job_dir / "main_lower_third.mp4"
        print("[composer] Adding lower-third graphic...")
        add_lower_third(
            merged, with_lower_third,
            title=script.get("lower_third_title", "")[:50],
            subtitle=script.get("lower_third_name", "NewscastAI Reporter"),
            start_sec=1.0, duration_sec=6.0
        )
        segments.append(with_lower_third)

    elif main_visual and not audio_sections.get("narration"):
        # No narration — use scroll video as silent background, still add lower third
        print("[composer] No narration audio — using scroll video as silent segment...")
        with_lower_third = job_dir / "main_lower_third.mp4"
        add_lower_third(
            main_visual, with_lower_third,
            title=script.get("lower_third_title", "")[:50],
            subtitle=script.get("lower_third_name", "NewscastAI Reporter"),
            start_sec=1.0, duration_sec=6.0
        )
        segments.append(with_lower_third)

    elif audio_sections.get("narration"):
        # No video — create a simple black screen with narration
        from narrator import get_audio_duration
        dur = get_audio_duration(Path(audio_sections["narration"]))
        black_bg = job_dir / "black_bg.mp4"
        headline_esc = script.get("headline", "")[:80].replace("'", "\\'").replace(":", "\\:")
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=black:size=1920x1080:rate=25:duration={dur}",
            "-i", audio_sections["narration"],
            "-vf", f"drawtext=text='{headline_esc}':fontsize=40:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(black_bg)
        ]
        subprocess.run(cmd, capture_output=True)
        segments.append(black_bg)

    # 3. Highlight clips from extracted videos (after main segment)
    for i, ev in enumerate(extracted_videos[:2]):
        if ev.get("success") and ev.get("path"):
            clip_path = Path(ev["path"])
            if clip_path.exists() and clip_path != main_visual:
                # Take first 20s of each highlight
                clip_out = job_dir / f"highlight_{i}.mp4"
                try:
                    from extractor import extract_clip
                    extract_clip(clip_path, 0, 20, clip_out)
                    segments.append(clip_out)
                    print(f"[composer] Added highlight clip {i+1}")
                except Exception as e:
                    print(f"[composer] Highlight clip failed: {e}")

    # 4. Add news ticker to all segments, then concat
    if len(segments) == 0:
        raise RuntimeError("No segments to compose!")

    headline = script.get("headline", script.get("source_title", "NewscastAI"))

    tickered_segments = []
    for i, seg in enumerate(segments):
        tickered = job_dir / f"tickered_{i}.mp4"
        try:
            add_news_ticker(seg, tickered, headline)
            tickered_segments.append(tickered)
        except Exception as e:
            print(f"[composer] Ticker failed for segment {i}: {e}")
            tickered_segments.append(seg)

    # 5. Concatenate all segments
    if len(tickered_segments) == 1:
        # Just copy the single segment
        import shutil
        shutil.copy(tickered_segments[0], final_output)
    else:
        print(f"[composer] Concatenating {len(tickered_segments)} segments...")
        concat_videos(tickered_segments, final_output)

    print(f"\n[composer] Final video: {final_output}")
    dur = get_video_duration(final_output)
    print(f"[composer] Duration: {dur:.1f}s")
    return final_output


if __name__ == "__main__":
    print("composer.py — run via pipeline.py or the MCP server")
