"""Script writing through Claude Code headless mode, so a Claude Pro/Max subscription works without an API key.

One-time setup (interactive, opens a browser):
    claude setup-token
then put the printed token in .env as CLAUDE_CODE_OAUTH_TOKEN. `claude -p` also works with a plain
`claude` login on machines where the CLI itself is logged in.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import env

log = logging.getLogger(__name__)


def find_claude_binary() -> str | None:
    """CLAUDE_CODE_BIN, then `claude` on PATH, then the newest copy bundled with the Claude desktop app."""
    explicit = env("CLAUDE_CODE_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    on_path = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    if on_path:
        return on_path
    appdata = os.environ.get("APPDATA", "")
    candidates = sorted(glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe")))
    return candidates[-1] if candidates else None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Accept bare JSON, JSON in a ```json fence, or JSON with prose around it."""
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def generate_json(system: str, prompt: str, schema: dict[str, Any], *, timeout: int = 600) -> dict[str, Any]:
    """Run one headless turn and return the structured output as a dict."""
    binary = find_claude_binary()
    if not binary:
        raise RuntimeError("Claude Code binary not found; set CLAUDE_CODE_BIN in .env or install Claude Code.")
    schema_text = json.dumps(schema)
    full_prompt = (prompt + "\n\nRespond with a single JSON object matching this JSON Schema and nothing else, "
                   "no code fence, no commentary:\n" + schema_text)
    cmd = [binary, "-p", full_prompt,
           "--output-format", "json",
           "--json-schema", schema_text,
           "--system-prompt", system,
           "--max-turns", "3",                 # structured output shows up as a tool_use stop; leave headroom
           "--tools", ""]                      # pure text generation, no file or shell tools
    child_env = dict(os.environ)
    child_env.pop("CLAUDECODE", None)           # allow running from inside another Claude Code session
    token = env("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        child_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    log.info("claude code: %s (token %s)", binary, "set" if token else "not set, using CLI login")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=child_env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Claude Code did not answer within {timeout}s") from exc
    raw = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    log.info("claude code exited %s; stdout %d chars, stderr %d chars", proc.returncode, len(raw), len(stderr))
    if stderr:
        log.info("claude code stderr: %s", stderr[:600])
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Claude Code returned non-JSON output (exit {proc.returncode}). "
                           f"stdout: {raw[:400]!r} stderr: {stderr[:400]!r}") from exc
    if isinstance(result, list):              # some versions emit an array of events; the result is last
        result = next((r for r in reversed(result) if isinstance(r, dict) and r.get("type") == "result"), result[-1])
    log.info("claude code result keys: %s | subtype=%s stop=%s", sorted(result.keys())[:14],
             result.get("subtype"), result.get("stop_reason"))
    if result.get("is_error"):
        msg = str(result.get("result", ""))
        if "authenticate" in msg.lower() or "oauth" in msg.lower():
            msg += ("\n  Run `claude setup-token` once in your own terminal (it opens a browser), then put the "
                    "token in .env as CLAUDE_CODE_OAUTH_TOKEN.")
        raise RuntimeError(f"Claude Code failed: {msg}")
    structured = result.get("structured_output")
    if isinstance(structured, str):
        structured = _extract_json_object(structured)
    if isinstance(structured, dict):
        return structured
    text = result.get("result")
    if not isinstance(text, str):
        text = json.dumps(text) if text is not None else ""
    log.info("claude code text result (first 400 chars): %r", text[:400])
    obj = _extract_json_object(text)
    if obj is None:
        raise RuntimeError("Claude Code answered but not with the script JSON. "
                           f"subtype={result.get('subtype')} stop_reason={result.get('stop_reason')} "
                           f"text starts: {text[:300]!r}")
    return obj
