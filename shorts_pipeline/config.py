from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
ASSETS_DIR = ROOT / "assets"

load_dotenv(ROOT / ".env")


LOCAL_CONFIG = ROOT / "config.local.yaml"   # untracked overlay written by the console; survives git pull


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
