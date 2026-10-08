"""Disk housekeeping: keep the downloaded-footage cache bounded and refuse to start a render on a full disk.

Every Pexels clip and photo the engine uses is cached under data/cache/pexels so a re-render does not
download it again. At ~11 MB per full-HD clip and 20 Shorts a day that grows by gigabytes a week, so the
cache is trimmed oldest-first (by last use) to `production.cache_max_gb` after every production.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from .config import DATA_DIR, ROOT, load_config
from .storage import CACHE_DIR

log = logging.getLogger(__name__)

MEDIA_CACHES = [CACHE_DIR / "pexels", CACHE_DIR / "aiimg"]
DEFAULT_CACHE_MAX_GB = 4.0
DEFAULT_MIN_FREE_GB = 3.0
GB = 1024 ** 3


def _limits() -> tuple[float, float]:
    prod = load_config().get("production") or {}
    return (float(prod.get("cache_max_gb", DEFAULT_CACHE_MAX_GB)), float(prod.get("min_free_gb", DEFAULT_MIN_FREE_GB)))


def free_gb(path: Path = ROOT) -> float:
    return shutil.disk_usage(path).free / GB


def touch(path: Path) -> None:
    """Mark a cached file as just used, so pruning keeps it longer."""
    try:
        now = time.time()
        os.utime(path, (now, now))
    except OSError:
        pass


def _media_files() -> list[tuple[float, int, Path]]:
    files = []
    for d in MEDIA_CACHES:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
    return files


def cache_size_gb() -> float:
    return sum(size for _, size, _ in _media_files()) / GB


def prune_cache(max_gb: float | None = None) -> tuple[int, float]:
    """Delete the least recently used cached media until the cache is under `max_gb`. Returns (files, GB freed)."""
    if max_gb is None:
        max_gb = _limits()[0]
    files = sorted(_media_files())                       # oldest use first
    total = sum(size for _, size, _ in files)
    limit = max(0.0, max_gb) * GB
    removed, freed = 0, 0
    for _, size, p in files:
        if total <= limit:
            break
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        freed += size
        removed += 1
    if removed:
        log.info("footage cache: removed %d least-recently-used files (%.1f GB); cache is now %.1f GB",
                 removed, freed / GB, total / GB)
    return removed, freed / GB


def ensure_free_space() -> None:
    """Called before a production: trim the cache if the disk is low, and stop early (with a clear message)
    rather than half-writing files when there is still not enough room."""
    max_gb, min_free = _limits()
    if free_gb() >= min_free:
        return
    log.warning("only %.1f GB free on the disk; trimming the footage cache", free_gb())
    prune_cache(max_gb)
    if free_gb() < min_free:
        prune_cache(min(max_gb, 1.0))
    if free_gb() < min_free:
        raise RuntimeError(f"only {free_gb():.1f} GB free on the disk (a Short needs about {min_free:.0f} GB to render "
                           f"safely). Free some space, e.g. delete old folders in output/, then run it again.")
