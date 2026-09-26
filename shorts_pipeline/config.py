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


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def has_typesafe() -> bool:
    return bool(env("TYPESAFE_API_KEY"))


def has_anthropic() -> bool:
    return bool(env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"))
