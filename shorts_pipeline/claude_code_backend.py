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

from .tools import run as _run
from .config import env

log = logging.getLogger(__name__)


def _version_key(path: str) -> tuple:
    """Sort key: the version folder number (2.1.286 > 2.1.99), then modification time."""
    m = re.search(r"claude-code[\\/](\d+)\.(\d+)\.(\d+)", path)
    ver = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return (ver, mtime)


def find_claude_binary() -> str | None:
    """CLAUDE_CODE_BIN, CLAUDE_CODE_EXECPATH, `claude` on PATH, then the newest copy bundled with the Claude
    desktop app (any depth under claude-code/, since updates changed the layout), then common installs."""
    for var in ("CLAUDE_CODE_BIN", "CLAUDE_CODE_EXECPATH"):
        explicit = env(var)
        if explicit and Path(explicit).exists():
            return explicit
    on_path = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    if on_path:
        return on_path
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    localappdata = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    patterns = [
        os.path.join(appdata, "Claude", "claude-code", "**", "claude.exe"),
        os.path.join(localappdata, "Programs", "claude-code", "**", "claude.exe"),
        os.path.join(home, ".claude", "local", "**", "claude.exe"),
        os.path.join(appdata, "npm", "claude.cmd"),
        os.path.join(home, ".local", "bin", "claude.exe"),
    ]
    found: list[str] = []
    for pat in patterns:
        found += glob.glob(pat, recursive=True)
    if not found:
        return None
    found.sort(key=_version_key)
    return found[-1]


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
    no_tools = ("\n\nYou have NO tools in this session: no web search, no file access, no shell. Do not try to call any. "
                "Answer from your own knowledge in one reply.")
    full_prompt = (prompt + "\n\nRespond with a single JSON object matching this JSON Schema and nothing else, "
                   "no code fence, no commentary:\n" + schema_text)
    child_env = dict(os.environ)
    child_env.pop("CLAUDECODE", None)           # allow running from inside another Claude Code session
    token = env("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        child_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    log.info("claude code: %s (token %s)", binary, "set" if token else "not set, using CLI login")

    result: dict[str, Any] = {}
    for attempt in (1, 2):
        cmd = [binary, "-p", full_prompt,
               "--output-format", "json",
               "--json-schema", schema_text,
               "--system-prompt", system + no_tools,
               "--max-turns", "6" if attempt == 1 else "10",   # the structured answer itself costs a tool_use turn
               "--tools", ""]                                  # pure text generation, no file or shell tools
        try:
            proc = _run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=timeout, env=child_env, retries=0)
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
        if isinstance(result, list):          # some versions emit an array of events; the result is last
            result = next((r for r in reversed(result) if isinstance(r, dict) and r.get("type") == "result"), result[-1])
        log.info("claude code result keys: %s | subtype=%s stop=%s turns=%s", sorted(result.keys())[:14],
                 result.get("subtype"), result.get("stop_reason"), result.get("num_turns"))
        if not result.get("is_error"):
            break
        details = {k: result.get(k) for k in ("subtype", "num_turns", "errors", "permission_denials") if result.get(k)}
        log.warning("claude code attempt %d failed: %s", attempt, json.dumps(details)[:600])
        if result.get("subtype") == "error_max_turns" and attempt == 1:
            full_prompt += "\n\nIMPORTANT: reply immediately with the JSON object. Do not call tools, do not search."
            continue
        break
    if result.get("is_error"):
        msg = str(result.get("result") or "")
        details = {k: result.get(k) for k in ("subtype", "num_turns", "errors", "permission_denials") if result.get(k)}
        if details:
            msg = (msg + " " if msg else "") + json.dumps(details)[:500]
        if "authenticate" in msg.lower() or "oauth" in msg.lower():
            msg += ("\n  Run `claude setup-token` once in your own terminal (it opens a browser), then put the "
                    "token in .env as CLAUDE_CODE_OAUTH_TOKEN.")
        if result.get("subtype") == "error_max_turns":
            msg += ("\n  The model kept trying to use tools instead of answering. If this repeats, check ~/.claude/settings.json "
                    "on this machine for hooks or permission rules that interfere with headless runs.")
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
