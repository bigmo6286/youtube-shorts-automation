from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SHARED_DIR

log = logging.getLogger(__name__)

RUNS_DIR = SHARED_DIR / "runs"      # trend runs are shared by every channel on this machine
CACHE_DIR = SHARED_DIR / "cache"    # footage, judgments, metadata: shared too


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
    """Atomic write: the data goes to a temporary file that replaces the target only once it is complete,
    so a full disk or a crash mid-write leaves the previous version instead of an empty file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):           # Windows refuses the swap while another process is reading the file
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                import time
                time.sleep(0.1)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file; an empty or corrupt file (e.g. written while the disk was full) counts as missing."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("ignoring unreadable %s (%s)", path, str(exc)[:80])
        return default


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
