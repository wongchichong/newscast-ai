"""
summarizer.py — LLM-powered article summarization and newscast script generation

Provider priority (auto-detected, or forced via LLM_PROVIDER env var):
  1. ANTHROPIC_API_KEY     → Claude API  (model: CLAUDE_API_MODEL, default: claude-haiku-4-5-20251001)
  2. GOOGLE_GEMINI_API_KEY → Gemini REST (model: GEMINI_API_MODEL,  default: gemini-2.0-flash)
  3. qodercli CLI          → model: QODERCLI_MODEL,   default: lite
  4. crush CLI             → model: CRUSH_MODEL,      default: (crush default)
  5. gemini CLI            → model: GEMINI_CLI_MODEL, default: gemini-2.5-flash
  6. claude CLI            → model: CLAUDE_CLI_MODEL, default: claude-haiku-4-5-20251001

All model defaults are overridable via environment variables.
Force a specific provider with: LLM_PROVIDER=qodercli (or claude/gemini/crush/gemini-cli/claude-cli)
"""

import os
import json
import shutil
import subprocess
from pathlib import Path

# ── Model defaults (all overridable via env) ──────────────────────────────────

CLAUDE_API_MODEL   = os.environ.get("CLAUDE_API_MODEL",   "claude-haiku-4-5-20251001")
GEMINI_API_MODEL   = os.environ.get("GEMINI_API_MODEL",   "gemini-2.0-flash")
QODERCLI_MODEL     = os.environ.get("QODERCLI_MODEL",     "lite")
CRUSH_MODEL        = os.environ.get("CRUSH_MODEL",        "")         # empty = crush default
GEMINI_CLI_MODEL   = os.environ.get("GEMINI_CLI_MODEL",   "gemini-2.5-flash")
CLAUDE_CLI_MODEL   = os.environ.get("CLAUDE_CLI_MODEL",   "claude-haiku-4-5-20251001")

GEMINI_REST_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/{model}:generateContent?key={api_key}"
)

# ── CLI availability ──────────────────────────────────────────────────────────

def _cli(name: str) -> bool:
    return shutil.which(name) is not None


# ── Provider auto-detection ───────────────────────────────────────────────────

def get_default_provider() -> str:
    """
    Auto-detect the best available LLM provider.
    Order: Claude API → Gemini API → qodercli → crush → gemini-cli → claude-cli
    Overridden by LLM_PROVIDER env var.
    """
    forced = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if forced:
        return forced

    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    if os.environ.get("GOOGLE_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if _cli("qodercli"):
        print(f"[summarizer] No API key — using qodercli (model: {QODERCLI_MODEL})")
        return "qodercli"
    if _cli("crush"):
        model_info = CRUSH_MODEL or "default"
        print(f"[summarizer] No API key — using crush (model: {model_info})")
        return "crush"
    if _cli("gemini"):
        print(f"[summarizer] No API key — using gemini CLI (model: {GEMINI_CLI_MODEL})")
        return "gemini-cli"
    if _cli("claude"):
        print(f"[summarizer] No API key — using claude CLI (model: {CLAUDE_CLI_MODEL})")
        return "claude-cli"

    raise RuntimeError(
        "No LLM available. Set ANTHROPIC_API_KEY or GOOGLE_GEMINI_API_KEY, "
        "or install one of: qodercli, crush, gemini CLI, claude CLI."
    )


# ── Claude API backend ────────────────────────────────────────────────────────

_claude_client = None

def _get_claude_client():
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic()
    return _claude_client


def _claude_api_complete(system: str, prompt: str, max_tokens: int = 1500) -> str:
    client = _get_claude_client()
    response = client.messages.create(
        model=CLAUDE_API_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ── Gemini REST API backend ───────────────────────────────────────────────────

def _gemini_api_complete(system: str, prompt: str, max_tokens: int = 1500) -> str:
    """Call Gemini via REST API — no SDK, no gRPC."""
    import requests as req
    import time
    api_key = os.environ.get("GOOGLE_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_GEMINI_API_KEY not set")

    url = GEMINI_REST_URL.format(model=GEMINI_API_MODEL, api_key=api_key)
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"{system}\n\n{prompt}"}]}
        ],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens},
    }
    for attempt in range(4):
        resp = req.post(url, json=payload, timeout=60)
        if resp.status_code == 429:
            wait = (attempt + 1) * 15
            print(f"  [gemini API] Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    raise RuntimeError("Gemini API rate limit exceeded after 4 retries")


# ── CLI backends ──────────────────────────────────────────────────────────────

def _run_cli(cmd: list[str], prompt: str, timeout: int = 120) -> str:
    """Run a CLI command with prompt, return stdout stripped."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} error: {result.stderr[:300]}")
    return result.stdout.strip()


def _qodercli_complete(system: str, prompt: str, max_tokens: int = 1500) -> str:
    full = f"{system}\n\n{prompt}"
    cmd = ["qodercli", "-p", full, "-f", "text", "--model", QODERCLI_MODEL]
    return _run_cli(cmd, full)


def _crush_complete(system: str, prompt: str, max_tokens: int = 1500) -> str:
    full = f"{system}\n\n{prompt}"
    cmd = ["crush", "run", "--quiet"]
    if CRUSH_MODEL:
        cmd += ["-m", CRUSH_MODEL]
    cmd.append(full)
    return _run_cli(cmd, full)


def _gemini_cli_complete(system: str, prompt: str, max_tokens: int = 1500) -> str:
    full = f"{system}\n\n{prompt}"
    cmd = ["gemini", "--prompt", full, "--output-format", "text", "-m", GEMINI_CLI_MODEL]
    return _run_cli(cmd, full)


def _claude_cli_complete(system: str, prompt: str, max_tokens: int = 1500) -> str:
    full = f"{system}\n\n{prompt}"
    cmd = [
        "claude", "-p", full,
        "--model", CLAUDE_CLI_MODEL,
        "--dangerously-skip-permissions",
    ]
    return _run_cli(cmd, full)


# ── Unified LLM call ──────────────────────────────────────────────────────────

SUMMARIZE_SYSTEM = """You are an expert news editor and broadcast journalist.
Your task is to analyze news articles and produce clear, accurate summaries and broadcast scripts.
Be factual, concise, and engaging. Write in broadcast journalism style."""


def llm_complete(prompt: str, system: str = SUMMARIZE_SYSTEM,
                 max_tokens: int = 1500, provider: str = None) -> str:
    """Call the configured LLM provider and return the text response."""
    if provider is None:
        provider = get_default_provider()

    p = provider.lower().strip()
    if p in ("claude", "anthropic"):
        return _claude_api_complete(system, prompt, max_tokens)
    elif p in ("gemini", "google"):
        return _gemini_api_complete(system, prompt, max_tokens)
    elif p == "qodercli":
        return _qodercli_complete(system, prompt, max_tokens)
    elif p == "crush":
        return _crush_complete(system, prompt, max_tokens)
    elif p == "gemini-cli":
        return _gemini_cli_complete(system, prompt, max_tokens)
    elif p in ("claude-cli", "claude_cli"):
        return _claude_cli_complete(system, prompt, max_tokens)
    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            "Valid: claude, gemini, qodercli, crush, gemini-cli, claude-cli"
        )


# ── Pipeline functions ────────────────────────────────────────────────────────

def summarize_article(title: str, text: str, max_words: int = 150,
                      provider: str = None) -> str:
    prompt = f"""Summarize this news article in {max_words} words or fewer.
Write in third person, present tense where appropriate. Be factual and concise.
Return plain text only — no markdown, no bullet points, no headers.

TITLE: {title}

ARTICLE:
{text[:4000]}

SUMMARY:"""
    return llm_complete(prompt, max_tokens=400, provider=provider)


def generate_newscast_script(
    title: str,
    summary: str,
    text: str,
    duration_seconds: int = 60,
    anchor_name: str = "Reporter",
    provider: str = None,
) -> dict:
    words_per_minute = 150
    target_words = int((duration_seconds / 60) * words_per_minute)

    prompt = f"""Create a professional TV newscast script for a {duration_seconds}-second segment (~{target_words} words of spoken content).

STORY TITLE: {title}

SUMMARY: {summary}

FULL TEXT:
{text[:5000]}

Return a JSON object with these exact keys:
{{
  "anchor_intro": "5-10 second intro line the anchor reads before the reporter segment",
  "headline": "One punchy headline sentence (for lower-third graphic)",
  "narration": "The main narration the reporter reads aloud — {target_words} words, broadcast style, clear and engaging",
  "key_facts": ["3-5 bullet point key facts for on-screen display"],
  "closing_line": "Brief 5-second closing sentence",
  "lower_third_title": "Short title for lower-third graphic (max 5 words)",
  "lower_third_name": "{anchor_name}",
  "estimated_duration_sec": {duration_seconds}
}}

Return ONLY the JSON object, no markdown fences, no explanation."""

    raw = llm_complete(prompt, max_tokens=1500, provider=provider)

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "anchor_intro": f"Our next story: {title}",
            "headline": title[:80],
            "narration": raw,
            "key_facts": [],
            "closing_line": "Reporting for NewscastAI.",
            "lower_third_title": title[:40],
            "lower_third_name": anchor_name,
            "estimated_duration_sec": duration_seconds,
        }


def generate_full_script(article_data: dict, duration_seconds: int = 90,
                         provider: str = None) -> dict:
    """Full pipeline: article dict → complete newscast script."""
    if provider is None:
        provider = get_default_provider()

    title = article_data.get("title", "Breaking News")
    text  = article_data.get("text", "")

    print(f"[summarizer] Provider: {provider}")
    print("[summarizer] Summarizing article...")
    summary = summarize_article(title, text, provider=provider)
    print(f"  Summary: {summary[:100]}...")

    print("[summarizer] Generating newscast script...")
    script = generate_newscast_script(title, summary, text, duration_seconds,
                                      provider=provider)
    script["summary"]      = summary
    script["source_title"] = title
    script["llm_provider"] = provider
    return script


if __name__ == "__main__":
    import sys
    provider = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"Using provider: {provider or get_default_provider()}")
    test_article = {
        "title": "Scientists Discover New Method to Generate Clean Energy",
        "text": (
            "Researchers at MIT have developed a breakthrough technology that could "
            "revolutionize clean energy production. The new method uses advanced solar cells "
            "combined with artificial intelligence to optimize energy capture, achieving 45% "
            "efficiency — nearly double current commercial panels. The team, led by Dr. Sarah Chen, "
            "published their findings in Nature Energy. The technology could be commercially "
            "available within 5 years and might reduce solar energy costs by 60%."
        ),
    }
    script = generate_full_script(test_article, duration_seconds=60, provider=provider)
    print(json.dumps(script, indent=2))
