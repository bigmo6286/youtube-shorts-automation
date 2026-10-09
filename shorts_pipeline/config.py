from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "assets"
# Shared by every channel on this machine: trend runs, footage/judgment caches, tools (ffmpeg), the channel registry.
SHARED_DIR = ROOT / "data"
CHANNELS_PATH = SHARED_DIR / "channels.json"

# One install can run several YouTube channels. The main channel lives at the top level (data/, output/, .env,
# config.local.yaml); every other channel has its own folder channels/<name>/ with the same layout, selected by
# `python main.py --channel <name> ...` (SHORTS_CHANNEL). Its .env overrides the main one (API keys are shared unless
# set again), and its config.local.yaml sits on config.yaml alone, so channel settings never leak between channels.
CHANNEL = re.sub(r"[^a-z0-9_-]", "", (os.environ.get("SHORTS_CHANNEL") or "").strip().lower())
HOME = ROOT / "channels" / CHANNEL if CHANNEL else ROOT
DATA_DIR = HOME / "data"
OUTPUT_DIR = HOME / "output"
ENV_PATH = HOME / ".env"

load_dotenv(ROOT / ".env")
if CHANNEL:
    load_dotenv(ENV_PATH, override=True)


LOCAL_CONFIG = HOME / "config.local.yaml"   # untracked overlay written by the console; survives git pull


def channels() -> list[dict[str, Any]]:
    """Registered channels on this machine; the main one is always first."""
    import json
    try:
        data = json.loads(CHANNELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    main = {"name": "main", "label": data.get("main_label") or "Main channel", "port": int(data.get("main_port") or 8787)}
    return [main] + [c for c in data.get("channels", []) if c.get("name") and c["name"] != "main"]


def channel_label() -> str:
    me = CHANNEL or "main"
    return next((c.get("label") or c["name"] for c in channels() if c["name"] == me), me)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    """config.yaml (tracked defaults) with config.local.yaml (this machine's settings) merged on top."""
    cfg_path = path or ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if path is None and LOCAL_CONFIG.exists():
        with open(LOCAL_CONFIG, "r", encoding="utf-8") as f:
            cfg = deep_merge(cfg, yaml.safe_load(f) or {})
    return cfg


def save_local_config(changes: dict[str, Any]) -> dict[str, Any]:
    """Merge `changes` into config.local.yaml and return the effective config."""
    current: dict[str, Any] = {}
    if LOCAL_CONFIG.exists():
        with open(LOCAL_CONFIG, "r", encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}
    merged = deep_merge(current, changes)
    with open(LOCAL_CONFIG, "w", encoding="utf-8") as f:
        f.write("# Settings saved by the web console for this machine. Not tracked by git; overrides config.yaml.\n")
        yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)
    return load_config()


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def has_typesafe() -> bool:
    return bool(env("TYPESAFE_API_KEY"))


def has_anthropic() -> bool:
    return bool(env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"))
