"""Free local script writer through Ollama (https://ollama.com): an open-weight model on this PC, no subscription.

Used as a fallback when Claude is unavailable (not installed, signed out, usage limit, network), or as the main
writer with `production.script_backend: ollama`. The model gets the same system prompt and must return JSON that
matches the same schema (Ollama's structured outputs); TypeSafe QA then judges the draft like any other.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

from .config import env, load_config

log = logging.getLogger(__name__)

DEFAULTS = {
    "fallback": True,                         # use Ollama when Claude is unavailable
    "model": "qwen2.5:3b",                    # small enough for a 2-core laptop; larger models write better
    "url": "http://127.0.0.1:11434",
    "timeout_seconds": 900,                   # CPU generation of one script can take several minutes
    "temperature": 0.8,
}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((load_config().get("production") or {}).get("ollama") or {})
    if env("OLLAMA_URL"):
        cfg["url"] = env("OLLAMA_URL")
    return cfg


def _url(path: str) -> str:
    return config()["url"].rstrip("/") + path


def installed_models() -> list[str]:
    try:
        r = requests.get(_url("/api/tags"), timeout=3)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def available() -> bool:
    """The Ollama server answers and the configured model is pulled."""
    want = config()["model"]
    names = installed_models()
    return any(n == want or n == f"{want}:latest" or n.split(":")[0] == want for n in names)


def status() -> dict[str, Any]:
    cfg = config()
    names = installed_models()
    return {"model": cfg["model"], "server": bool(names) or _server_up(), "model_ready": available(),
            "fallback": bool(cfg["fallback"]), "models": names}


def _server_up() -> bool:
    try:
        return requests.get(_url("/api/version"), timeout=3).ok
    except Exception:  # noqa: BLE001
        return False


# Hard limits for small local models: without them they can keep writing until the timeout.
LIMITS = {"title": 90, "hook": 160, "text": 150, "visual_keyword": 60, "cta": 120, "description": 400,
          "visual_fallback": 50, "thumbnail_text": 40, "lines": (5, 12), "hashtags": (3, 6)}
MAX_TOKENS = 900


def _inline_refs(schema: dict[str, Any], limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve $ref/$defs so small models and the grammar converter see one plain schema; drop schema metadata
    (titles of schemas, not properties named "title"); add length limits per property name."""
    defs = schema.get("$defs") or schema.get("definitions") or {}
    limits = limits or {}

    def walk(node: Any, name: str = "") -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(dict(defs.get(node["$ref"].split("/")[-1], {})), name)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in ("$defs", "definitions", "title"):
                continue                                   # metadata; real properties live under "properties"
            if k == "properties" and isinstance(v, dict):
                out[k] = {prop: walk(sub, prop) for prop, sub in v.items()}
            else:
                out[k] = walk(v)
        lim = limits.get(name)
        if lim is not None and out.get("type") == "string" and isinstance(lim, int):
            out["maxLength"] = lim
        if lim is not None and out.get("type") == "array" and isinstance(lim, tuple):
            out["minItems"], out["maxItems"] = lim
        if name == "hashtags" and out.get("type") == "array":
            out.setdefault("items", {})["maxLength"] = 30
        return out
    return walk(schema)


def _thinks(model: str) -> bool:
    m = model.lower()
    return m.startswith(("qwen3", "deepseek-r1", "magistral")) or ":thinking" in m


def generate_json(system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    cfg = config()
    plain = _inline_refs(schema, LIMITS)
    body = {
        "model": cfg["model"], "stream": False, "format": plain, "keep_alive": "10m",
        "options": {"temperature": float(cfg["temperature"]), "num_ctx": 8192, "num_predict": MAX_TOKENS},
        "messages": [
            {"role": "system", "content": system + "\n\nAnswer with one JSON object only, matching the required schema."},
            {"role": "user", "content": prompt},
        ],
    }
    if _thinks(cfg["model"]):
        body["think"] = False          # reasoning models (qwen3.x, deepseek-r1) would spend minutes "thinking" first
    started = time.time()
    try:
        r = requests.post(_url("/api/chat"), json=body, timeout=float(cfg["timeout_seconds"]))
        if r.status_code == 400 and "think" in r.text.lower() and "think" in body:
            body.pop("think")          # this model or Ollama version does not accept the switch
            r = requests.post(_url("/api/chat"), json=body, timeout=float(cfg["timeout_seconds"]))
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama is not reachable at {cfg['url']} ({exc}); is the Ollama app running?") from exc
    if r.status_code != 200:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:300]}")
    data = r.json()
    text = (data.get("message") or {}).get("content") or ""
    log.info("ollama %s answered in %.0f s (%s tokens)", cfg["model"], time.time() - started, data.get("eval_count", "?"))
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError(f"Ollama returned no JSON: {text[:200]!r}")
