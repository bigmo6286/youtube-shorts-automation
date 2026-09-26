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
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    appdata = os.environ.get("APPDATA", "")
    candidates = sorted(glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe")))
    return candidates[-1] if candidates else None


def generate_json(system: str, prompt: str, schema: dict[str, Any], *, timeout: int = 600) -> dict[str, Any]:
    """Run one headless turn and return the structured output as a dict."""
    binary = find_claude_binary()
    if not binary:
        raise RuntimeError("Claude Code binary not found; set CLAUDE_CODE_BIN in .env or install Claude Code.")
    cmd = [binary, "-p", prompt,
           "--output-format", "json",
           "--json-schema", json.dumps(schema),
           "--system-prompt", system,
           "--max-turns", "1",
           "--tools", ""]                      # pure text generation, no file or shell tools
    child_env = dict(os.environ)
    child_env.pop("CLAUDECODE", None)           # allow running from inside another Claude Code session
    if env("CLAUDE_CODE_OAUTH_TOKEN"):
        child_env["CLAUDE_CODE_OAUTH_TOKEN"] = env("CLAUDE_CODE_OAUTH_TOKEN")  # type: ignore[assignment]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=child_env)
    raw = proc.stdout.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Claude Code returned non-JSON output: {raw[:300]} {proc.stderr[:300]}") from exc
    if result.get("is_error"):
        msg = result.get("result", "")
        if "authenticate" in msg.lower() or "oauth" in msg.lower():
            msg += ("\n  Run `claude setup-token` once in your own terminal (it opens a browser), then put the "
                    "token in .env as CLAUDE_CODE_OAUTH_TOKEN.")
        raise RuntimeError(f"Claude Code failed: {msg}")
    structured = result.get("structured_output")
    if isinstance(structured, dict):
        return structured
    text = result.get("result", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Claude Code did not return JSON matching the schema: {text[:300]}") from exc
