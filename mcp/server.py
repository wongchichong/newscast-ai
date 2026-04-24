"""
NewscastAI MCP Server — minimal JSON-RPC 2.0 over stdio (no pydantic/mcp package needed)
Exposes newscast pipeline tools so Claude Code and subagents can call them.
"""

import sys
import json
import os
from pathlib import Path

# Add tools dir to path
TOOLS_DIR = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

TOOLS = [
    {
        "name": "scrape_article",
        "description": "Fetch a news article URL and extract text, images, and build a scroll video.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "News article URL to scrape"},
                "job_id": {"type": "string", "description": "Optional job ID for file organization"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "extract_videos",
        "description": "Extract and download embedded videos from a news article page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "job_id": {"type": "string"},
            },
            "required": ["url", "job_id"],
        },
    },
    {
        "name": "generate_script",
        "description": "Use an LLM (Claude or Gemini) to summarize an article and generate a newscast script.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string"},
                "duration_seconds": {"type": "integer", "default": 90},
                "llm_provider": {
                    "type": "string",
                    "description": "'claude' or 'gemini'. Auto-detected from env if omitted.",
                    "enum": ["claude", "gemini", "gemini-cli"],
                },
            },
            "required": ["title", "text"],
        },
    },
    {
        "name": "generate_narration",
        "description": "Convert text to speech audio using edge-tts. Returns path to MP3 file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "output_name": {"type": "string"},
                "voice_key": {"type": "string", "default": "male_us"},
                "job_id": {"type": "string"},
            },
            "required": ["text", "job_id"],
        },
    },
    {
        "name": "compose_video",
        "description": "Compose the final newscast video from all generated assets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "script": {"type": "object"},
                "scroll_video": {"type": "string"},
                "extracted_videos": {"type": "array"},
                "audio_sections": {"type": "object"},
                "output_filename": {"type": "string"},
            },
            "required": ["job_id", "script", "audio_sections"],
        },
    },
    {
        "name": "run_full_pipeline",
        "description": (
            "Run the complete end-to-end newscast generation pipeline: "
            "scrape → extract videos → generate script → narrate → compose video. "
            "Returns path to final MP4."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "duration_seconds": {"type": "integer", "default": 90},
                "voice_key": {"type": "string", "default": "male_us"},
                "output_filename": {"type": "string"},
                "llm_provider": {
                    "type": "string",
                    "description": "LLM to use for script generation: 'claude' or 'gemini'. Auto-detected from env if omitted.",
                    "enum": ["claude", "gemini", "gemini-cli"],
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "run_category_pipeline",
        "description": (
            "Run the multi-country category-based newscast pipeline. "
            "Searches news across countries on a topic, aggregates with LLM, "
            "generates infographics, narrates, records, and composes a broadcast MP4."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "News category or topic (e.g. 'AI news', 'climate change', 'war in Ukraine')",
                },
                "countries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of country codes to search (e.g. ['us','uk','cn']). Defaults to all major countries.",
                },
                "max_per_country": {
                    "type": "integer",
                    "default": 2,
                    "description": "Max articles per country",
                },
                "voice_key": {"type": "string", "default": "male_us"},
                "target_duration": {
                    "type": "integer",
                    "default": 120,
                    "description": "Target video duration in seconds",
                },
                "output_filename": {"type": "string"},
                "job_id": {"type": "string"},
                "llm_provider": {
                    "type": "string",
                    "description": "LLM provider override: claude, gemini, qodercli, crush, gemini-cli, claude-cli",
                },
            },
            "required": ["category"],
        },
    },
    # ── Pipeline v2 (NotebookLM-powered) ────────────────────────────────
    {
        "name": "run_category_pipeline_v2",
        "description": (
            "Run the NotebookLM-powered newscast pipeline: web search (multi-country) → "
            "video search (multi-platform) → NotebookLM ingest → highlights → "
            "Playwright highlight videos → yt-dlp clips → infographic → narrative (>30 min) → "
            "NotebookLM video overview → MP4."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Topic/title for the newscast"},
                "countries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Country codes to search (e.g. ['us','uk','cn','jp','de','fr','ru','in','br','kr','au','ca'])",
                },
                "max_per_country": {"type": "integer", "default": 15},
                "output_filename": {"type": "string"},
                "job_id": {"type": "string"},
                "resume_from": {"type": "string", "description": "Resume from a job ID checkpoint"},
                "skip_stages": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Stage numbers to skip (1-10)",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "search_web",
        "description": "Stage 1: Multi-country web search. Returns article URLs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "countries": {"type": "array", "items": {"type": "string"}},
                "max_per_country": {"type": "integer", "default": 15},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "search_video",
        "description": "Stage 2: Multi-platform video search (YouTube, Bilibili, Dailymotion, Vimeo). Returns video URLs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "platforms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Platforms: youtube, bilibili, dailymotion, vimeo, reddit, tiktok",
                },
                "max_per_platform": {"type": "integer", "default": 10},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "notebooklm_ingest",
        "description": "Stage 3: Create NotebookLM notebook and ingest article/video URLs as sources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notebook title"},
                "article_urls": {"type": "array", "items": {"type": "string"}},
                "video_urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    },
    {
        "name": "generate_highlights",
        "description": "Stage 4: Ask NotebookLM to generate key highlights from web article sources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notebook_id": {"type": "string"},
            },
            "required": ["notebook_id"],
        },
    },
    {
        "name": "record_highlights",
        "description": "Stage 5: Record Playwright highlight videos and upload to NotebookLM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "highlights": {"type": "array", "description": "List of {page_url, highlight_text, context}"},
                "notebook_id": {"type": "string", "description": "To upload highlight videos as sources"},
                "job_id": {"type": "string"},
            },
            "required": ["highlights", "job_id"],
        },
    },
    {
        "name": "extract_clips",
        "description": "Stage 7: Use yt-dlp to extract video clips by timestamp from NotebookLM video highlights.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_highlights": {"type": "array", "description": "List of {video_url, caption, timestamp_start, timestamp_end, duration}"},
                "notebook_id": {"type": "string", "description": "To upload clips as sources"},
                "job_id": {"type": "string"},
            },
            "required": ["video_highlights", "job_id"],
        },
    },
    {
        "name": "generate_infographic",
        "description": "Stage 8: Generate infographic via NotebookLM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notebook_id": {"type": "string"},
                "job_id": {"type": "string"},
                "instructions": {"type": "string"},
            },
            "required": ["notebook_id", "job_id"],
        },
    },
    {
        "name": "generate_narrative",
        "description": "Stage 9: Generate long-form narrative audio (>30 min) and transcript via NotebookLM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notebook_id": {"type": "string"},
                "job_id": {"type": "string"},
                "min_duration_minutes": {"type": "integer", "default": 30},
            },
            "required": ["notebook_id", "job_id"],
        },
    },
    {
        "name": "generate_overview_video",
        "description": "Stage 10: Generate NotebookLM video overview based on all collected sources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notebook_id": {"type": "string"},
                "title": {"type": "string"},
                "job_id": {"type": "string"},
            },
            "required": ["notebook_id", "job_id"],
        },
    },
]


def call_tool(name: str, arguments: dict) -> str:
    """Dispatch a tool call and return JSON result string."""
    try:
        if name == "scrape_article":
            from scraper import scrape_to_video
            import uuid, time
            job_id = arguments.get("job_id", f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}")
            result = scrape_to_video(arguments["url"], job_id)
            result["job_id"] = job_id
            return json.dumps(result)

        elif name == "extract_videos":
            from extractor import extract_videos_from_page
            from scraper import fetch_article
            article = fetch_article(arguments["url"])
            result = extract_videos_from_page(arguments["url"], article.get("html", ""), arguments["job_id"])
            return json.dumps(result)

        elif name == "generate_script":
            from summarizer import generate_full_script
            article_data = {"title": arguments["title"], "text": arguments["text"]}
            result = generate_full_script(article_data, arguments.get("duration_seconds", 90),
                                          provider=arguments.get("llm_provider"))
            return json.dumps(result)

        elif name == "generate_narration":
            from narrator import generate_narration
            import time, uuid
            job_id = arguments.get("job_id", f"job_{int(time.time())}")
            out_name = arguments.get("output_name", "narration")
            out_path = Path(__file__).parent.parent / "temp" / job_id / "audio" / f"{out_name}.mp3"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            path = generate_narration(arguments["text"], out_path,
                                      voice_key=arguments.get("voice_key", "male_us"))
            return json.dumps({"audio_path": str(path)})

        elif name == "compose_video":
            from composer import compose_newscast
            result = compose_newscast(
                job_id=arguments["job_id"],
                script=arguments["script"],
                scroll_video=arguments.get("scroll_video"),
                extracted_videos=arguments.get("extracted_videos", []),
                audio_sections=arguments["audio_sections"],
                output_filename=arguments.get("output_filename"),
            )
            return json.dumps({"output_video": str(result)})

        elif name == "run_full_pipeline":
            from pipeline import run_pipeline
            result = run_pipeline(
                url=arguments["url"],
                duration_seconds=arguments.get("duration_seconds", 90),
                voice_key=arguments.get("voice_key", "male_us"),
                output_filename=arguments.get("output_filename"),
                llm_provider=arguments.get("llm_provider"),
            )
            return json.dumps(result)

        elif name == "run_category_pipeline":
            from category_pipeline import run_category_pipeline
            result = run_category_pipeline(
                category=arguments["category"],
                countries=arguments.get("countries"),
                max_per_country=arguments.get("max_per_country", 2),
                voice_key=arguments.get("voice_key", "male_us"),
                target_duration=arguments.get("target_duration", 120),
                output_filename=arguments.get("output_filename"),
                job_id=arguments.get("job_id"),
                llm_provider=arguments.get("llm_provider"),
            )
            return json.dumps(result, default=str)

        # ── Pipeline v2 (NotebookLM-powered) ────────────────────────

        elif name == "run_category_pipeline_v2":
            from category_pipeline_v2 import run_category_pipeline_v2
            result = run_category_pipeline_v2(
                title=arguments["title"],
                countries=arguments.get("countries"),
                max_per_country=arguments.get("max_per_country", 15),
                output_filename=arguments.get("output_filename"),
                job_id=arguments.get("job_id"),
                resume_from=arguments.get("resume_from"),
                skip_stages=arguments.get("skip_stages", []),
            )
            return json.dumps(result, default=str)

        elif name == "search_web":
            from search_web import search_web
            result = search_web(
                topic=arguments["topic"],
                countries=arguments.get("countries"),
                max_per_country=arguments.get("max_per_country", 15),
            )
            return json.dumps(result, default=str)

        elif name == "search_video":
            from search_video import search_video
            result = search_video(
                topic=arguments["topic"],
                platforms=arguments.get("platforms"),
                max_per_platform=arguments.get("max_per_platform", 10),
            )
            return json.dumps(result, default=str)

        elif name == "notebooklm_ingest":
            import uuid, time
            from notebooklm_integration import create_notebook, add_sources_batch
            notebook_id = None
            # Check if there's an existing job with a notebook
            job_id = arguments.get("job_id", f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}")
            nb = create_notebook(arguments.get("title", job_id))
            notebook_id = nb["id"]

            all_urls = []
            all_urls.extend(arguments.get("article_urls", [])[:100])
            all_urls.extend(arguments.get("video_urls", [])[:50])

            source_results = add_sources_batch(notebook_id, all_urls, concurrency=5)
            return json.dumps({
                "notebook_id": notebook_id,
                "source_ids": [r["source_id"] for r in source_results if r.get("source_id")],
                "total_sources": len([r for r in source_results if r.get("source_id")]),
            }, default=str)

        elif name == "generate_highlights":
            from notebooklm_integration import ask_for_structured
            result = ask_for_structured(
                arguments["notebook_id"],
                (
                    "Analyze all web article sources (URLs, not videos). "
                    "Identify the key highlights and important text passages. "
                    "For each highlight, provide: the source page URL, the exact highlight text (2-4 sentences), "
                    "and brief context about why it matters. "
                    "Return as a JSON array with objects: {page_url, highlight_text, context}. "
                    "Aim for 10-20 high-quality highlights."
                ),
                "json",
            )
            return json.dumps(result, default=str)

        elif name == "record_highlights":
            import asyncio
            from highlight_recorder import record_highlights
            from notebooklm_integration import add_source
            highlights = arguments["highlights"]
            job_id = arguments["job_id"]
            job_dir = Path(__file__).parent.parent / "temp" / job_id
            highlight_dir = job_dir / "highlight_videos"

            results = record_highlights(highlights, highlight_dir, duration_per_highlight=8.0)
            successful_paths = [r["path"] for r in results if r.get("success") and r.get("path")]

            new_source_ids = []
            notebook_id = arguments.get("notebook_id")
            if notebook_id:
                for path in successful_paths:
                    if Path(path).exists():
                        r = add_source(notebook_id, path)
                        if r.get("source_id"):
                            new_source_ids.append(r["source_id"])

            return json.dumps({
                "highlight_videos": successful_paths,
                "new_source_ids": new_source_ids,
                "count": len(successful_paths),
            }, default=str)

        elif name == "extract_clips":
            from clip_extractor import extract_clips_batch
            video_highlights = arguments["video_highlights"]
            job_id = arguments["job_id"]
            job_dir = Path(__file__).parent.parent / "temp" / job_id
            clip_dir = job_dir / "clips"

            results = extract_clips_batch(video_highlights, clip_dir, quality="720p")
            clip_paths = [r["path"] for r in results if r.get("success") and r.get("path")]

            new_source_ids = []
            notebook_id = arguments.get("notebook_id")
            if notebook_id:
                from notebooklm_integration import add_source
                for path in clip_paths:
                    if Path(path).exists():
                        r = add_source(notebook_id, path)
                        if r.get("source_id"):
                            new_source_ids.append(r["source_id"])

            return json.dumps({
                "clips": clip_paths,
                "new_source_ids": new_source_ids,
                "count": len(clip_paths),
            }, default=str)

        elif name == "generate_infographic":
            from notebooklm_integration import generate_infographic, wait_for_artifact, download_infographic
            notebook_id = arguments["notebook_id"]
            job_id = arguments["job_id"]
            job_dir = Path(__file__).parent.parent / "temp" / job_id

            artifact = generate_infographic(
                notebook_id,
                instructions=arguments.get("instructions", "Create comprehensive infographics covering key data, comparisons, and timelines"),
            )
            artifact_id = artifact.get("task_id", "")
            if not artifact_id:
                return json.dumps({"success": False, "error": "Failed to start generation"})

            status = wait_for_artifact(notebook_id, artifact_id, timeout=1200)
            if status == "completed":
                output_path = job_dir / "infographic.png"
                ok = download_infographic(notebook_id, artifact_id, str(output_path))
                return json.dumps({"success": ok, "artifact_id": artifact_id, "path": str(output_path) if ok else None})
            return json.dumps({"success": False, "artifact_id": artifact_id, "status": status})

        elif name == "generate_narrative":
            from notebooklm_integration import generate_audio, wait_for_artifact, download_audio, ask
            notebook_id = arguments["notebook_id"]
            job_id = arguments["job_id"]
            job_dir = Path(__file__).parent.parent / "temp" / job_id
            min_duration = arguments.get("min_duration_minutes", 30)

            artifact = generate_audio(
                notebook_id,
                instructions=(
                    f"Create a comprehensive deep-dive podcast covering ALL topics and sources. "
                    f"Target duration: at least {min_duration} minutes. "
                    f"Cover every angle thoroughly."
                ),
                format_type="deep-dive",
                length="long",
            )
            artifact_id = artifact.get("task_id", "")
            if not artifact_id:
                return json.dumps({"success": False, "error": "Failed to start generation"})

            status = wait_for_artifact(notebook_id, artifact_id, timeout=1800)
            audio_path = None
            transcript = None

            if status == "completed":
                output_path = job_dir / "narrative.mp3"
                ok = download_audio(notebook_id, artifact_id, str(output_path))
                if ok:
                    audio_path = str(output_path)
                transcript_result = ask(notebook_id, "Provide the full transcript of the audio overview.")
                transcript = transcript_result.get("answer", "")

            return json.dumps({
                "success": audio_path is not None,
                "artifact_id": artifact_id,
                "audio_path": audio_path,
                "transcript": transcript[:2000] if transcript else None,
                "status": status,
            }, default=str)

        elif name == "generate_overview_video":
            from notebooklm_integration import generate_video, wait_for_artifact, download_video
            notebook_id = arguments["notebook_id"]
            title = arguments.get("title", "")
            job_id = arguments["job_id"]
            job_dir = Path(__file__).parent.parent / "temp" / job_id

            artifact = generate_video(
                notebook_id,
                instructions=(
                    f"Create a comprehensive video explainer for: {title}. "
                    f"Use ALL available sources to create a well-structured visual narrative."
                ),
                format_type="explainer",
                style="auto",
            )
            artifact_id = artifact.get("task_id", "")
            if not artifact_id:
                return json.dumps({"success": False, "error": "Failed to start generation"})

            status = wait_for_artifact(notebook_id, artifact_id, timeout=2700)
            if status == "completed":
                output_path = job_dir / "overview.mp4"
                ok = download_video(notebook_id, artifact_id, str(output_path))
                return json.dumps({
                    "success": ok,
                    "artifact_id": artifact_id,
                    "path": str(output_path) if ok else None,
                    "status": status,
                })
            return json.dumps({"success": False, "artifact_id": artifact_id, "status": status})

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        import traceback
        return json.dumps({"error": str(e), "traceback": traceback.format_exc()})


def send(obj: dict):
    """Write a JSON-RPC message to stdout."""
    line = json.dumps(obj)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def handle_request(req: dict) -> dict | None:
    """Handle a single JSON-RPC 2.0 request."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "newscast-ai", "version": "2.0.0"},
            }
        }

    elif method == "notifications/initialized":
        return None  # notification, no response

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": TOOLS}
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result_text = call_tool(tool_name, arguments)
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "content": [{"type": "text", "text": result_text}],
                "isError": False,
            }
        }

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    else:
        if req_id is not None:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }
        return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "Parse error"}})
            continue

        response = handle_request(req)
        if response is not None:
            send(response)


if __name__ == "__main__":
    main()
