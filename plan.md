# Newscast-AI v2 — NotebookLM-Powered Pipeline

## Architecture

```
Title → Web Search (multi-country) → Video Search (yt-dlp, all platforms)
      → NotebookLM (source ingest) → Highlights → Playwright Highlight Videos
      → yt-dlp Clip Extraction → Infographic → Narrative (>30 min)
      → NotebookLM Video Generation → Final MP4
```

## Pipeline Stages

### Stage 0: Input
- **Input**: Topic/title string (e.g. "US-China trade war 2025")
- **Output**: Search query strings for web + video search

### Stage 1: Multi-Country Web Search
- **Module**: `search_web.py`
- **What it does**:
  - Search Google News across all configured countries (reuses `news_search.py` COUNTRY config)
  - Extract article URLs, titles, snippets
  - Use NotebookLM's `add-research --mode deep` for broader web coverage
- **Output**: List of article URLs (50-200 URLs across countries)

### Stage 2: Multi-Platform Video Search
- **Module**: `search_video.py`
- **What it does**:
  - Search YouTube via yt-dlp (`ytsearchN:query`)
  - Search Bilibili via yt-dlp (`bilisearchN:query`)
  - Search Vimeo, Dailymotion, and other yt-dlp supported platforms
  - Collect video URLs, titles, durations, thumbnails
- **Output**: List of video URLs (30-100 videos across platforms)

### Stage 3: NotebookLM Source Ingestion
- **Module**: `notebooklm_integration.py`
- **What it does**:
  - Create NotebookLM notebook: `"Newscast: {title}"`
  - Batch-add all article URLs as sources
  - Batch-add all video URLs as sources (YouTube, Bilibili, etc.)
  - Wait for all sources to be processed (READY status)
- **Output**: notebook_id, source_ids list

### Stage 4: Generate Highlights from Pages
- **Module**: `notebooklm_integration.py`
- **What it does**:
  - Ask NotebookLM: "Generate key highlights from all web article sources. Return as JSON with: page_url, highlight_text, context"
  - Parse highlights — structured data with source URLs and text excerpts
- **Output**: highlights JSON (list of {page_url, highlight_text, context})

### Stage 5: Playwright Highlight Video Recording
- **Module**: `highlight_recorder.py`
- **What it does**:
  - For each highlight from Stage 4:
    - Navigate to the page_url
    - Find the highlight_text on the page
    - Record a video that scrolls/zooms/pans to the text
    - Draw a highlight overlay on the text (like the existing `pw-zoom-target` CSS)
    - Save as `highlighted_text_{N}.mp4`
  - Upload all highlight videos back to NotebookLM as sources
- **Output**: List of highlight video files + new source_ids in NotebookLM
- **Reuses**: `playwright_scraper.py` CSS + zoom logic

### Stage 6: Extract Video Highlights with Timestamps
- **Module**: `notebooklm_integration.py`
- **What it does**:
  - Ask NotebookLM: "For all video sources, provide highlights with: video_url, caption_text, timestamp_start, timestamp_end, duration"
  - Parse structured video highlight data
- **Output**: video_highlights JSON (list of {video_url, caption, timestamp_start, timestamp_end, duration})

### Stage 7: yt-dlp Clip Extraction
- **Module**: `clip_extractor.py`
- **What it does**:
  - For each video_highlight entry:
    - Use yt-dlp `--download-sections` to extract the exact timestamp range
    - Save as `highlighted_caption_{N}.mp4`
  - Upload all clips back to NotebookLM as sources
- **Output**: List of clip files + new source_ids in NotebookLM
- **Reuses**: `youtube_search.py` yt-dlp download logic

### Stage 8: Generate Infographics
- **Module**: `notebooklm_integration.py`
- **What it does**:
  - Ask NotebookLM: `generate infographic` with instructions to cover key data points, comparisons, timelines
  - Wait for artifact completion
  - Download infographic(s) as PNG
- **Output**: Infographic PNG file(s)

### Stage 9: Generate Long-Form Narrative (>30 min)
- **Module**: `notebooklm_integration.py`
- **What it does**:
  - Ask NotebookLM: `generate audio` with instructions for comprehensive coverage, >30 min narration
  - Format: deep-dive or brief depending on style preference
  - Wait for artifact completion
  - Download audio as MP3
  - Also fetch the transcript via `ask "Provide full transcript"`
- **Output**: Audio MP3 file + transcript text

### Stage 10: NotebookLM Video Overview Generation
- **Module**: `notebooklm_integration.py`
- **What it does**:
  - Ask NotebookLM: `generate video` with custom prompt referencing all collected sources
  - Prompt includes: narrative structure, key visuals from highlights, infographic references
  - Wait for artifact completion
  - Download video as MP4
- **Output**: NotebookLM generated video MP4

### Stage 11: Final Composition (Optional)
- **Module**: `composer_v2.py` (new, or reuse existing `composer.py`)
- **What it does**:
  - Combine NotebookLM video overview with additional assets if needed
  - Add title card, lower thirds, news ticker (broadcast styling)
  - Sync with long-form narration audio if replacing audio track
- **Output**: Final broadcast-ready MP4

## File Structure

```
tools/
  search_web.py          # Stage 1: Multi-country web search
  search_video.py        # Stage 2: Multi-platform video search
  notebooklm_integration.py  # Stages 3,4,6,8,9,10: All NotebookLM interactions
  highlight_recorder.py  # Stage 5: Playwright highlight video recording
  clip_extractor.py      # Stage 7: yt-dlp timestamp-based clip extraction
  composer_v2.py         # Stage 11: Final video composition (optional)
  category_pipeline_v2.py  # New orchestrator for the full pipeline

reused from v1:
  news_search.py         # COUNTRY config, Google News RSS search
  playwright_scraper.py  # Playwright setup, CSS, zoom logic
  youtube_search.py      # yt-dlp wrapper functions
  composer.py            # Video composition utilities
```

## MCP Server Tools (new)

| Tool | Description |
|------|-------------|
| `run_category_pipeline_v2` | Full new pipeline: topic → search → ingest → generate → compose |
| `search_web` | Stage 1: multi-country web search, returns URLs |
| `search_video` | Stage 2: multi-platform video search, returns URLs |
| `notebooklm_ingest` | Stage 3: create notebook, add all sources |
| `generate_highlights` | Stage 4: get highlights from NotebookLM |
| `record_highlights` | Stage 5: Playwright highlight videos |
| `extract_clips` | Stage 7: yt-dlp clip extraction by timestamp |
| `generate_infographic` | Stage 8: NotebookLM infographic |
| `generate_narrative` | Stage 9: NotebookLM long-form audio + transcript |
| `generate_overview_video` | Stage 10: NotebookLM video generation |

## Checkpoint & Resume

Like `category_pipeline.py`, the new pipeline saves checkpoints:
- `_checkpoint.json` in each job temp dir
- Tracks stages_done, notebook_id, source_ids, artifact_ids
- Can resume from any failed stage

## Error Handling

- Each stage is independent — degraded output if one fails
- NotebookLM rate limits: retry with exponential backoff
- Playwright failures: fall back to simple scroll video
- yt-dlp failures: skip unavailable videos
- Source processing failures: continue with remaining sources
