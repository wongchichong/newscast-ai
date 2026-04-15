"""
narrator.py — Text-to-speech narration generation
Primary:  Kokoro ONNX (local, CPU, high quality)
Fallback: edge-tts (Microsoft Neural, cloud)
"""

import json
import subprocess
import os
from pathlib import Path

TEMP_DIR = Path(__file__).parent.parent / "temp"

# Kokoro model paths (downloaded once, reused)
KOKORO_MODEL  = Path(os.environ.get("KOKORO_MODEL",  "/root/kokoro-v1.0.int8.onnx"))
KOKORO_VOICES = Path(os.environ.get("KOKORO_VOICES", "/root/voices-v1.0.bin"))

# Voice mapping: key → (kokoro_voice, edge_tts_voice)
VOICE_MAP = {
    "male_us":   ("am_michael", "en-US-GuyNeural"),
    "female_us": ("af_jessica", "en-US-JennyNeural"),
    "male_uk":   ("bm_george",  "en-GB-RyanNeural"),
    "female_uk": ("bf_emma",    "en-GB-SoniaNeural"),
    "male_au":   ("am_adam",    "en-AU-WilliamNeural"),
    "female_au": ("af_nova",    "en-AU-NatashaNeural"),
}

DEFAULT_VOICE_KEY = "male_us"

# Singleton Kokoro instance (loaded once per process)
_kokoro_instance = None


def _get_kokoro():
    """Load and cache the Kokoro model (lazy singleton)."""
    global _kokoro_instance
    if _kokoro_instance is not None:
        return _kokoro_instance
    if not KOKORO_MODEL.exists():
        raise FileNotFoundError(f"Kokoro model not found: {KOKORO_MODEL}")
    if not KOKORO_VOICES.exists():
        raise FileNotFoundError(f"Kokoro voices not found: {KOKORO_VOICES}")

    # Disable ONNX runtime thread affinity which breaks in PRoot containers
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OMP_WAIT_POLICY"] = "PASSIVE"

    from kokoro_onnx import Kokoro
    print(f"[narrator] Loading Kokoro model: {KOKORO_MODEL.name}")
    _kokoro_instance = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
    return _kokoro_instance


def _tts_kokoro(text: str, output_path: Path, voice_key: str = "male_us",
                speed: float = 1.0) -> Path:
    """
    Generate TTS with Kokoro ONNX. Saves as WAV then converts to MP3 via ffmpeg.
    speed < 1.0 = slightly slower (better for news broadcast style).
    """
    import soundfile as sf

    kokoro_voice = VOICE_MAP.get(voice_key, VOICE_MAP[DEFAULT_VOICE_KEY])[0]
    k = _get_kokoro()

    samples, sr = k.create(text, voice=kokoro_voice, speed=speed, lang="en-us")

    wav_path = output_path.with_suffix(".wav")
    sf.write(str(wav_path), samples, sr)

    # Convert WAV → MP3, resampling to 44100 Hz for broad player compatibility
    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(wav_path),
        "-ar", "44100",
        "-c:a", "libmp3lame", "-q:a", "2",
        str(output_path)
    ], capture_output=True, text=True)
    wav_path.unlink(missing_ok=True)

    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg wav→mp3 failed: {r.stderr[-200:]}")
    return output_path


async def _tts_edge(text: str, output_path: Path, voice: str,
                    rate: str = "+0%", pitch: str = "+0Hz"):
    """Generate TTS audio using edge-tts (fallback)."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await communicate.save(str(output_path))


def _tts_edge_sync(text: str, output_path: Path, voice_key: str = "male_us") -> Path:
    """Sync wrapper for edge-tts fallback."""
    import asyncio
    edge_voice = VOICE_MAP.get(voice_key, VOICE_MAP[DEFAULT_VOICE_KEY])[1]
    asyncio.run(_tts_edge(text, output_path, edge_voice, rate="-5%", pitch="-5Hz"))
    return output_path


def generate_narration(
    text: str,
    output_path: Path,
    voice_key: str = "male_us",
    speed: float = 1.0,
) -> Path:
    """
    Generate narration audio from text.
    Tries Kokoro first; falls back to edge-tts on failure.
    Returns path to .mp3.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not str(output_path).endswith(".mp3"):
        output_path = output_path.with_suffix(".mp3")

    kokoro_voice = VOICE_MAP.get(voice_key, VOICE_MAP[DEFAULT_VOICE_KEY])[0]
    print(f"[narrator] Kokoro TTS voice={kokoro_voice}  len={len(text)}")

    try:
        _tts_kokoro(text, output_path, voice_key=voice_key, speed=speed)
        print(f"[narrator] Audio saved: {output_path}")
        return output_path
    except Exception as e:
        print(f"[narrator] Kokoro failed: {e} — falling back to edge-tts")

    try:
        _tts_edge_sync(text, output_path, voice_key=voice_key)
        print(f"[narrator] Audio saved (edge-tts): {output_path}")
        return output_path
    except Exception as e:
        raise RuntimeError(f"All TTS methods failed. Last error: {e}")


def get_audio_duration(audio_path: Path) -> float:
    """Get duration of audio file in seconds using ffprobe."""
    result = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", str(audio_path)
    ], capture_output=True, text=True)
    data = json.loads(result.stdout)
    for stream in data.get("streams", []):
        duration = stream.get("duration")
        if duration:
            return float(duration)
    return 0.0


def generate_all_narrations(script: dict, job_id: str, voice_key: str = "male_us") -> dict:
    """
    Generate audio for all script sections (single-URL pipeline).
    Returns dict mapping section → audio path.
    """
    job_dir = TEMP_DIR / job_id / "audio"
    job_dir.mkdir(parents=True, exist_ok=True)
    sections = {}

    def _try(text: str, filename: str, vkey: str) -> str | None:
        if not text or len(text.strip()) < 10:
            return None
        try:
            path = job_dir / filename
            generate_narration(text, path, voice_key=vkey)
            return str(path)
        except Exception as e:
            print(f"  [narrator] {filename} failed: {e}")
            return None

    if script.get("anchor_intro"):
        p = _try(script["anchor_intro"], "anchor_intro.mp3", "female_us")
        if p:
            sections["anchor_intro"] = p

    if script.get("narration"):
        p = _try(script["narration"], "narration.mp3", voice_key)
        if p:
            sections["narration"] = p
        else:
            print("  [narrator] WARNING: main narration failed — video will be silent")

    if script.get("closing_line"):
        p = _try(script["closing_line"], "closing.mp3", voice_key)
        if p:
            sections["closing"] = p

    return sections


def generate_segment_narrations(
    segments: list[dict],
    job_id: str,
    voice_key: str = "male_us",
) -> list[dict]:
    """
    Generate TTS audio for each narration segment (category pipeline).
    Returns segments with added audio_path and duration keys.
    """
    job_dir = TEMP_DIR / job_id / "audio"
    job_dir.mkdir(parents=True, exist_ok=True)

    out = []
    for i, seg in enumerate(segments):
        text = (seg.get("text") or "").strip()
        seg_type = seg.get("type", "segment")
        filename = f"seg_{i:02d}_{seg_type}.mp3"
        result = dict(seg)

        if not text or len(text) < 10:
            print(f"  [narrator] Skipping segment {i} ({seg_type}): too short")
            result["audio_path"] = None
            result["duration"] = 0.0
            out.append(result)
            continue

        audio_path = job_dir / filename
        try:
            generate_narration(text, audio_path, voice_key=voice_key)
            dur = get_audio_duration(audio_path)
            result["audio_path"] = str(audio_path)
            result["duration"] = dur
            print(f"  [narrator] Segment {i} ({seg_type}): {dur:.1f}s")
        except Exception as e:
            print(f"  [narrator] Segment {i} ({seg_type}) failed: {e}")
            result["audio_path"] = None
            result["duration"] = 0.0

        out.append(result)

    total = sum(s["duration"] for s in out)
    print(f"  [narrator] Total: {total:.1f}s across {len(out)} segments")
    return out


if __name__ == "__main__":
    test_text = (
        "Breaking news: Scientists at MIT have made a groundbreaking discovery "
        "that could change renewable energy forever. The new solar technology "
        "achieves forty-five percent efficiency — nearly double current panels. "
        "Commercial availability is expected within five years."
    )
    out = TEMP_DIR / "test_narration.mp3"
    out.parent.mkdir(parents=True, exist_ok=True)
    generate_narration(test_text, out)
    duration = get_audio_duration(out)
    print(f"Duration: {duration:.1f}s")
