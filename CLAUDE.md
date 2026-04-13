# NewscastAI — Agent Instructions

## Overview
Automated newscast video generator. Two modes:
- **Single-URL pipeline**: takes a news article URL and produces a broadcast-ready MP4.
- **Category pipeline**: searches a topic across multiple countries, aggregates with LLM, generates infographics, and produces a multi-segment MP4.

## Quick Start

### Single-URL pipeline (CLI):
```bash
python3 ~/newscast-ai/tools/pipeline.py <URL> [duration_sec] [voice_key]
```
Example:
```bash
python3 ~/newscast-ai/tools/pipeline.py https://bbc.com/news/... 90 male_us
```

### Category pipeline (CLI):
```bash
python3 ~/newscast-ai/tools/category_pipeline.py --category "AI news" [--countries us,uk,cn] [--duration 120] [--voice male_us]
```
Example:
```bash
python3 ~/newscast-ai/tools/category_pipeline.py --category "climate change" --countries us,uk,cn,jp --duration 150
```

### Via MCP tools (preferred for agents):
Use the `newscast-ai` MCP server. Available tools:
- `run_full_pipeline` — single URL: scrape → extract → script → narrate → compose
- `run_category_pipeline` — topic-based: search across countries → aggregate → infographics → narrate → compose
- `scrape_article` — stage 1: fetch + scroll video
- `extract_videos` — stage 2: download embedded videos
- `generate_script` — stage 3: LLM script generation
- `generate_narration` — stage 4: TTS audio
- `compose_video` — stage 5: final MP4

## Pipeline Stages

**Single-URL:**
```
URL → scrape_article → extract_videos → generate_script → generate_narration → compose_video → MP4
```

**Category:**
```
topic → news_search (multi-country) → aggregate_articles (LLM) → generate_infographics
      → generate_segment_narrations → record_segments (Playwright) → compose_category → MP4
```

## Voice Options
| Key | Voice |
|-----|-------|
| male_us | en-US-GuyNeural |
| female_us | en-US-JennyNeural |
| male_uk | en-GB-RyanNeural |
| female_uk | en-GB-SoniaNeural |
| male_au | en-AU-WilliamNeural |
| female_au | en-AU-NatashaNeural |

## Output
- Videos saved to: `~/newscast-ai/output/newscast_<job_id>.mp4`
- Format: 1920x1080, H.264, AAC, MP4
- Temp files: `~/newscast-ai/temp/<job_id>/`

## Requirements
- `ANTHROPIC_API_KEY` or `GOOGLE_GEMINI_API_KEY` for LLM script generation (or install qodercli/crush/gemini-cli/claude CLI as fallback)
- ffmpeg must be installed (`pkg install ffmpeg`)
- Python packages: see `requirements.txt`
- Kokoro TTS model: `~/kokoro-v1.0.int8.onnx` + `~/voices-v1.0.bin` (primary TTS; falls back to edge-tts)
- Playwright + Chromium: for infographic recording and URL scroll videos

## Agent Chain
- **Orchestrator** (`agents/orchestrator.md`) — manages the full pipeline, spawns subagents
- **ScraperAgent** (`agents/scraper_agent.md`) — handles stage 1
- **ScriptAgent** (`agents/script_agent.md`) — handles stage 3
- **ComposerAgent** (`agents/composer_agent.md`) — handles stage 5

## Error Handling
Each stage is independent — if one fails, the pipeline continues with degraded output:
- No images → title card + audio only
- No embedded videos → scroll video only  
- No API key → narration only (no LLM script)
- No TTS → silent video with captions
