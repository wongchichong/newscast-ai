"""
notebooklm_integration.py — NotebookLM integration for newscast pipeline v2.

Wraps the notebooklm-py CLI for all NotebookLM interactions:
  - Notebook creation
  - Source ingestion (URLs, files)
  - Chat / queries
  - Artifact generation (audio, video, infographic, report, etc.)
  - Artifact download
  - Source waiting / status checking
"""

import json
import subprocess
import os
import time
from pathlib import Path
from typing import Optional
from datetime import datetime

# ── CLI wrapper ───────────────────────────────────────────────────────────────

def _run_cmd(args: list[str], timeout: int = 120, parse_json: bool = False):
    """Run a notebooklm CLI command and return result."""
    cmd = ["notebooklm"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and "error" in r.stderr.lower():
            return None, r.stderr.strip()
        if parse_json and r.stdout.strip():
            try:
                return json.loads(r.stdout.strip()), None
            except json.JSONDecodeError:
                return r.stdout.strip(), None
        return r.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return None, "notebooklm CLI not found. Install: pip install notebooklm-py"


# ── Authentication ────────────────────────────────────────────────────────────

def check_auth() -> dict:
    """Check NotebookLM authentication status."""
    # status may return "No notebook selected" which is fine — not an auth error
    _run_cmd(["status"], timeout=10)

    output_auth, err_auth = _run_cmd(["auth", "check", "--json"], timeout=10, parse_json=True)
    if output_auth and isinstance(output_auth, dict):
        # CLI returns "status": "ok" when authenticated
        if output_auth.get("status") == "ok":
            return {
                "authenticated": True,
                "checks": output_auth.get("checks", {}),
                "details": output_auth.get("details", {}),
            }
        return {
            "authenticated": False,
            "checks": output_auth.get("checks", {}),
            "error": output_auth.get("error"),
        }

    # Fallback: check if storage file exists with cookies
    checks = output_auth.get("checks", {}) if output_auth and isinstance(output_auth, dict) else {}
    return {
        "authenticated": checks.get("storage_exists", False) and checks.get("cookies_present", False),
        "checks": checks,
    }


# ── Notebook Management ───────────────────────────────────────────────────────

def create_notebook(title: str) -> dict:
    """Create a new NotebookLM notebook. Returns {id, title}."""
    output, err = _run_cmd(["create", title, "--json"], timeout=30, parse_json=True)
    if err:
        raise RuntimeError(f"Failed to create notebook: {err}")
    # Output is {"notebook": {"id": ..., "title": ...}}
    if isinstance(output, dict) and "notebook" in output:
        return output["notebook"]
    return output


def list_notebooks() -> list[dict]:
    """List all notebooks."""
    output, err = _run_cmd(["list", "--json"], timeout=30, parse_json=True)
    if output and isinstance(output, dict):
        return output.get("notebooks", [])
    return []


def delete_notebook(notebook_id: str):
    """Delete a notebook."""
    _run_cmd(["notebook", "delete", notebook_id], timeout=30)


# ── Source Management ─────────────────────────────────────────────────────────

def add_source(notebook_id: str, url_or_path: str, wait: bool = False) -> dict:
    """
    Add a single source (URL or file path) to a notebook.
    Returns {source_id, title, status}.
    """
    output, err = _run_cmd(
        ["source", "add", url_or_path, "-n", notebook_id, "--json"],
        timeout=60, parse_json=True,
    )
    if err:
        print(f"  [nblm] Source add warning: {err}")
        return {"source_id": None, "title": url_or_path, "status": "error", "error": err}

    if wait and output.get("source_id"):
        wait_for_source(notebook_id, output["source_id"], timeout=120)
    return output


def add_sources_batch(notebook_id: str, urls: list[str], concurrency: int = 5, max_retries: int = 2) -> list[dict]:
    """
    Add multiple sources to a notebook with retries on rate limits.
    Returns list of {source_id, title, status}.
    """
    results = []
    print(f"  [nblm] Adding {len(urls)} sources to notebook {notebook_id[:8]}...")

    for url in urls:
        success = False
        for attempt in range(max_retries + 1):
            result = add_source(notebook_id, url, wait=False)
            if result.get("source_id"):
                title_safe = result.get('title', url)[:60].encode('ascii', 'replace').decode('ascii')
                print(f"    [{len(results)+1}/{len(urls)}] {title_safe} -> {result.get('source_id', 'N/A')[:12]}")
                results.append(result)
                success = True
                break
            else:
                if attempt < max_retries:
                    wait_time = 3 * (attempt + 1)
                    time.sleep(wait_time)
                else:
                    title_safe = url[:60].encode('ascii', 'replace').decode('ascii')
                    print(f"    [{len(results)+1}/{len(urls)}] {title_safe} -> error (after {max_retries+1} tries)")
                    results.append(result)

        # Small delay between each source to avoid rate limiting
        time.sleep(1.5)

    # Wait for all sources to be ready
    source_ids = [r["source_id"] for r in results if r.get("source_id")]
    if source_ids:
        print(f"  [nblm] Waiting for {len(source_ids)} sources to process...")
        wait_for_sources(notebook_id, source_ids, timeout=300)

    return results


def wait_for_source(notebook_id: str, source_id: str, timeout: int = 120) -> str:
    """Wait for a single source to be READY. Returns final status."""
    output, err = _run_cmd(
        ["source", "wait", source_id, "-n", notebook_id, "--timeout", str(timeout)],
        timeout=timeout + 30,
    )
    if err:
        return "timeout" if "timed out" in err.lower() else "error"
    return "ready"


def wait_for_sources(notebook_id: str, source_ids: list[str], timeout: int = 300):
    """Wait for multiple sources to be ready. Polls status."""
    deadline = time.time() + timeout
    remaining = list(source_ids)

    while remaining and time.time() < deadline:
        ready_ids = []
        for sid in remaining:
            status = _get_source_status(notebook_id, sid)
            if status in ("ready", "READY", "processed"):
                ready_ids.append(sid)

        for sid in ready_ids:
            remaining.remove(sid)

        if remaining:
            print(f"  [nblm] Waiting for {len(remaining)} sources... ({len(ready_ids)} ready)")
            time.sleep(10)

    if remaining:
        print(f"  [nblm] {len(remaining)} sources still processing after timeout")
    else:
        print(f"  [nblm] All {len(source_ids)} sources ready")


def _get_source_status(notebook_id: str, source_id: str) -> str:
    """Get status of a single source."""
    output, err = _run_cmd(
        ["source", "list", "-n", notebook_id, "--json"],
        timeout=30, parse_json=True,
    )
    if output and isinstance(output, dict):
        for s in output.get("sources", []):
            if s.get("id", "").startswith(source_id[:8]):
                return s.get("status", "unknown")
    return "unknown"


def list_sources(notebook_id: str) -> list[dict]:
    """List all sources in a notebook."""
    output, err = _run_cmd(
        ["source", "list", "-n", notebook_id, "--json"],
        timeout=30, parse_json=True,
    )
    if output and isinstance(output, dict):
        return output.get("sources", [])
    return []


# ── Chat / Queries ────────────────────────────────────────────────────────────

def ask(notebook_id: str, question: str, sources: list[str] = None) -> dict:
    """
    Ask NotebookLM a question.
    Returns {answer, conversation_id, turn_number, references}.
    """
    args = ["ask", question, "-n", notebook_id, "--json"]
    if sources:
        for s in sources:
            args.insert(-1, "-s")
            args.insert(-1, s)

    output, err = _run_cmd(args, timeout=60, parse_json=True)
    if err:
        return {"answer": None, "error": err}
    return output if isinstance(output, dict) else {}


def ask_for_structured(notebook_id: str, prompt: str, output_format: str = "json") -> dict:
    """
    Ask NotebookLM a question with explicit output format instructions.
    Wraps the prompt with format requirements.
    """
    formatted_prompt = f"{prompt}\n\nRespond in valid {output_format.upper()} format only, no markdown wrapper."
    return ask(notebook_id, formatted_prompt)


# ── Artifact Generation ───────────────────────────────────────────────────────

def generate_artifact(
    notebook_id: str,
    artifact_type: str,  # audio, video, infographic, report, quiz, flashcards, mind-map, data-table
    instructions: str = None,
    extra_args: list[str] = None,
    timeout: int = 60,
) -> dict:
    """
    Generate an artifact in NotebookLM.
    Returns {task_id, status}.
    """
    args = ["generate", artifact_type]
    if instructions:
        args.append(instructions)
    args.extend(["-n", notebook_id, "--json"])
    if extra_args:
        args.extend(extra_args)

    output, err = _run_cmd(args, timeout=timeout, parse_json=True)
    if err:
        raise RuntimeError(f"Generate {artifact_type} failed: {err}")
    return output if isinstance(output, dict) else {}


def generate_audio(
    notebook_id: str,
    instructions: str = "Create a comprehensive deep-dive podcast covering all key topics",
    format_type: str = "deep-dive",
    length: str = "long",
) -> dict:
    """Generate audio overview (podcast)."""
    return generate_artifact(
        notebook_id, "audio", instructions,
        extra_args=["--format", format_type, "--length", length],
    )


def generate_video(
    notebook_id: str,
    instructions: str = "Create a comprehensive video explainer covering all topics with visuals",
    format_type: str = "explainer",
    style: str = "auto",
) -> dict:
    """Generate video explainer."""
    return generate_artifact(
        notebook_id, "video", instructions,
        extra_args=["--format", format_type, "--style", style],
    )


def generate_infographic(
    notebook_id: str,
    instructions: str = "Create detailed infographics covering key data, comparisons, and timelines",
    orientation: str = "landscape",
    detail: str = "detailed",
    style: str = "professional",
) -> dict:
    """Generate infographic."""
    return generate_artifact(
        notebook_id, "infographic", instructions,
        extra_args=["--orientation", orientation, "--detail", detail, "--style", style],
    )


def generate_report(
    notebook_id: str,
    format_type: str = "briefing-doc",
    append_instructions: str = None,
) -> dict:
    """Generate a report/briefing doc."""
    extra = ["--format", format_type]
    if append_instructions:
        extra.extend(["--append", append_instructions])
    return generate_artifact(notebook_id, "report", extra_args=extra)


# ── Artifact Status & Download ────────────────────────────────────────────────

def list_artifacts(notebook_id: str) -> list[dict]:
    """List all artifacts in a notebook."""
    output, err = _run_cmd(
        ["artifact", "list", "-n", notebook_id, "--json"],
        timeout=30, parse_json=True,
    )
    if output and isinstance(output, dict):
        return output.get("artifacts", [])
    return []


def wait_for_artifact(
    notebook_id: str,
    artifact_id: str,
    timeout: int = 1800,
) -> str:
    """
    Wait for an artifact to complete. Returns final status.
    Exit code 0 = completed, 2 = timeout, 1 = error.
    """
    _, err = _run_cmd(
        ["artifact", "wait", artifact_id, "-n", notebook_id, "--timeout", str(timeout)],
        timeout=timeout + 60,
    )
    if err:
        if "timed out" in err.lower():
            return "timeout"
        return "error"
    return "completed"


def download_audio(notebook_id: str, artifact_id: str, output_path: str) -> bool:
    """Download audio artifact to local file."""
    _, err = _run_cmd(
        ["download", "audio", output_path, "-a", artifact_id, "-n", notebook_id],
        timeout=120,
    )
    if err:
        print(f"  [nblm] Download audio warning: {err}")
        return False
    return Path(output_path).exists()


def download_video(notebook_id: str, artifact_id: str, output_path: str) -> bool:
    """Download video artifact to local file."""
    _, err = _run_cmd(
        ["download", "video", output_path, "-a", artifact_id, "-n", notebook_id],
        timeout=120,
    )
    if err:
        print(f"  [nblm] Download video warning: {err}")
        return False
    return Path(output_path).exists()


def download_infographic(notebook_id: str, artifact_id: str, output_path: str) -> bool:
    """Download infographic artifact to local file."""
    _, err = _run_cmd(
        ["download", "infographic", output_path, "-a", artifact_id, "-n", notebook_id],
        timeout=120,
    )
    if err:
        print(f"  [nblm] Download infographic warning: {err}")
        return False
    return Path(output_path).exists()


def download_report(notebook_id: str, artifact_id: str, output_path: str) -> bool:
    """Download report artifact to local file."""
    _, err = _run_cmd(
        ["download", "report", output_path, "-a", artifact_id, "-n", notebook_id],
        timeout=120,
    )
    if err:
        print(f"  [nblm] Download report warning: {err}")
        return False
    return Path(output_path).exists()


# ── Research ──────────────────────────────────────────────────────────────────

def add_research(
    notebook_id: str,
    query: str,
    mode: str = "deep",
    wait: bool = True,
    import_all: bool = True,
    timeout: int = 600,
) -> dict:
    """
    Run web research and import sources into notebook.
    mode: "fast" or "deep"
    """
    args = ["source", "add-research", query, "--mode", mode]
    if not wait:
        args.append("--no-wait")
    if import_all:
        args.append("--import-all")
    args.extend(["-n", notebook_id, "--json"])

    output, err = _run_cmd(args, timeout=timeout, parse_json=True)
    if err:
        return {"status": "error", "error": err}

    if wait:
        _, _ = _run_cmd(
            ["research", "wait", "-n", notebook_id, "--timeout", str(timeout),
             "--import-all" if import_all else ""],
            timeout=timeout + 60,
        )

    return output if isinstance(output, dict) else {}


# ── Deep research workflow (recommended for Stage 1 supplementation) ──────────

def deep_research(
    notebook_id: str,
    topic: str,
    timeout: int = 600,
) -> int:
    """
    Run deep web research and return number of sources imported.
    """
    print(f"  [nblm] Starting deep research: {topic}")
    add_research(notebook_id, topic, mode="deep", wait=False, import_all=True)

    print(f"  [nblm] Waiting for research to complete (up to {timeout}s)...")
    _, _ = _run_cmd(
        ["research", "wait", "-n", notebook_id, "--import-all", "--timeout", str(timeout)],
        timeout=timeout + 120,
    )

    sources = list_sources(notebook_id)
    print(f"  [nblm] Research complete. Total sources: {len(sources)}")
    return len(sources)
