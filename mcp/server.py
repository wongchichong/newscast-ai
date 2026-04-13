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
                "serverInfo": {"name": "newscast-ai", "version": "1.0.0"},
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
