# NewscastAI

Automated newscast video generator. Turns any news article or topic into a broadcast-ready MP4 with narration, lower-thirds, news ticker, and synced visual footage — no manual editing required.

---

## Features

- **Single-URL pipeline** — give it any news article URL, get a full newscast video
- **Category pipeline** — search a topic across multiple countries, aggregate into a multi-segment global newscast
- **Kokoro TTS** — high-quality local text-to-speech, 6 voice options across US/UK/AU accents
- **Synced browser recording** — Playwright records a live scroll of the source article, duration locked to the narration audio
- **HTML infographics** — Chart.js country coverage charts, comparison tables, and timelines (category mode)
- **Broadcast overlays** — title card, lower-third graphic, scrolling news ticker
- **LLM auto-detection** — uses Claude API, Gemini API, or local CLI tools (qodercli, crush, gemini-cli, claude-cli)
- **MCP server** — expose all pipeline tools to Claude Code agents

---

## Quick Start

### Single-URL pipeline
```bash
python3 tools/pipeline.py <URL> [duration_sec] [voice_key]
```
```bash
python3 tools/pipeline.py https://www.bbc.com/news/articles/... 60 male_us
```

### Category pipeline
```bash
python3 tools/category_pipeline.py --category "AI news" [--countries us,uk,cn] [--duration 120] [--voice male_us]
```
```bash
python3 tools/category_pipeline.py --category "climate change" --countries us,uk,cn,jp --duration 150
```

---

## Pipeline Stages

### Single-URL
```
URL → scrape article → extract embedded videos → generate script (LLM)
    → narrate (Kokoro TTS) → record scroll video (Playwright) → compose MP4
```

### Category
```
topic → search news (multi-country RSS) → aggregate articles (LLM)
      → generate infographics (HTML/Chart.js) → narrate segments (Kokoro TTS)
      → record segments (Playwright) → compose MP4
```

---

## Output

- **Format:** 1920×1080, H.264, AAC 44100 Hz, MP4
- **Location:** `output/newscast_<job_id>.mp4`
- **Typical duration:** 60–150s depending on `--duration` setting
- Temp files written to `temp/<job_id>/` and cleaned up after use

---

## Voice Options

| Key | Voice |
|-----|-------|
| `male_us` | en-US (Guy) |
| `female_us` | en-US (Jenny) |
| `male_uk` | en-GB (Ryan) |
| `female_uk` | en-GB (Sonia) |
| `male_au` | en-AU (William) |
| `female_au` | en-AU (Natasha) |

---

## Visual Elements

| Element | Details |
|---------|---------|
| Title card | 3s black intro with topic and headline |
| Scroll video | Live Playwright recording of source article, synced to narration length |
| Lower third | Black bar with topic title + desk name, fades in at 0.5s |
| News ticker | Red scrolling bar at bottom with headline text |
| Infographics | Country coverage badges, bar charts, vertical timelines (category mode only) |

---

## MCP Server

Expose the pipeline as tools for Claude Code agents:

```bash
python3 mcp/server.py
```

Available tools:

| Tool | Description |
|------|-------------|
| `run_full_pipeline` | Single-URL end-to-end pipeline |
| `run_category_pipeline` | Multi-country category pipeline |
| `scrape_article` | Stage 1: fetch article text + scroll video |
| `extract_videos` | Stage 2: download embedded videos |
| `generate_script` | Stage 3: LLM script generation |
| `generate_narration` | Stage 4: TTS audio |
| `compose_video` | Stage 5: final MP4 composition |

---

## Requirements

- **ffmpeg** — `pkg install ffmpeg`
- **Python 3.10+** — `pip install -r requirements.txt`
- **Playwright + Chromium** — `playwright install chromium`
- **Kokoro TTS model** — `kokoro-v1.0.int8.onnx` + `voices-v1.0.bin` in `~/`
- **LLM** — set `ANTHROPIC_API_KEY` or `GOOGLE_GEMINI_API_KEY`, or install a supported CLI tool

### Install

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## LLM Provider Priority

Auto-detected in this order:

1. `ANTHROPIC_API_KEY` → Claude API
2. `GOOGLE_GEMINI_API_KEY` → Gemini API
3. `qodercli` CLI
4. `crush` CLI
5. `gemini` CLI
6. `claude` CLI

Force a specific provider:
```bash
LLM_PROVIDER=gemini python3 tools/pipeline.py <URL>
```

---

## Project Structure

```
newscast-ai/
├── tools/
│   ├── pipeline.py           # Single-URL orchestrator
│   ├── category_pipeline.py  # Category orchestrator
│   ├── scraper.py            # Article fetch
│   ├── extractor.py          # Embedded video download
│   ├── summarizer.py         # LLM script generation
│   ├── narrator.py           # Kokoro TTS + edge-tts fallback
│   ├── composer.py           # ffmpeg video composition
│   ├── playwright_scraper.py # Browser recording
│   ├── news_search.py        # Multi-country RSS search
│   ├── aggregator.py         # Multi-article LLM aggregation
│   └── infographic.py        # HTML infographic generation
├── mcp/
│   └── server.py             # MCP JSON-RPC server
├── agents/                   # Agent instruction files
├── output/                   # Generated MP4s
├── temp/                     # Per-job working files
└── requirements.txt
```

---

## Notes

- Run one pipeline at a time — concurrent Playwright instances will crash each other in constrained environments
- TTS speed is set to 0.82× for broadcast pacing (~116 wpm)
- Audio is resampled to 44100 Hz for broad player compatibility
