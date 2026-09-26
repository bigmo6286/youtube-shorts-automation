from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR

RUNS_DIR = DATA_DIR / "runs"
CACHE_DIR = DATA_DIR / "cache"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def new_run_dir() -> Path:
    d = RUNS_DIR / now_iso()
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_run_dir() -> Path | None:
    if not RUNS_DIR.exists():
        return None
    runs = sorted(p for p in RUNS_DIR.iterdir() if p.is_dir() and (p / "shorts.json").exists())
    return runs[-1] if runs else None


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class JsonCache:
    """Tiny on-disk cache keyed by string, one JSON file per namespace. Thread-safe: the
    fetch and judge stages write to it from worker threads."""

    def __init__(self, name: str):
        self.path = CACHE_DIR / f"{name}.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = load_json(self.path, {}) or {}

    def get(self, key: str) -> Any:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            tmp = self.path.with_suffix(".json.tmp")
            save_json(tmp, self._data)
            tmp.replace(self.path)
