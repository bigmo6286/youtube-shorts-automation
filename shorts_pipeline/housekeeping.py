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
from typing import Any

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


# ---------------------------------------------------------------------------------------------------- output folders
# Render pieces: only needed while the Short is being made. The final video, its script, details and thumbnail stay.
INTERMEDIATE_PATTERNS = ("bg_*.mp4", "bg_*.jpg", "bg_*.png", "outro_bg.mp4", "intro_bg.mp4", "voice.mp3", "*.ass",
                         "words.json", "*.tmp", ".*.tmp")
KEEP_FILES = {"short.mp4", "meta.json", "script.json", "thumbnail.jpg"}
CLEANUP_DEFAULTS = {"clean_after_upload": True, "keep_video_days": 3, "keep_unuploaded_days": 30}


def cleanup_config() -> dict[str, Any]:
    cfg = dict(CLEANUP_DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("cleanup")) or {})
    return cfg


def _remove(paths: list[Path], dry_run: bool) -> int:
    freed = 0
    for p in paths:
        try:
            size = p.stat().st_size
            if not dry_run:
                p.unlink()
            freed += size
        except OSError:
            continue
    return freed


def remove_intermediates(out_dir: Path, dry_run: bool = False) -> int:
    """Delete the render pieces of one Short (it is finished). Returns bytes freed."""
    files = {p for pattern in INTERMEDIATE_PATTERNS for p in out_dir.glob(pattern) if p.is_file()}
    return _remove(sorted(f for f in files if f.name not in KEEP_FILES), dry_run)


def cleanup_outputs(dry_run: bool = False, now: float | None = None) -> dict[str, Any]:
    """Uploaded Shorts lose their render pieces at once and their video file after keep_video_days on YouTube
    (YouTube keeps the video); Shorts never uploaded keep everything for keep_unuploaded_days. The script, details
    and thumbnail always stay, so history, analytics and the console cards keep working."""
    from .config import OUTPUT_DIR
    from .storage import load_json, save_json

    cfg = cleanup_config()
    now = now or time.time()
    stats = {"folders": 0, "pieces_bytes": 0, "videos_removed": 0, "video_bytes": 0, "dry_run": dry_run}
    if not OUTPUT_DIR.exists():
        return stats
    for d in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        meta = load_json(d / "meta.json") or {}
        made = d.stat().st_mtime
        try:
            made = time.mktime(time.strptime(d.name[:19], "%Y-%m-%dT%H-%M-%S")) - time.timezone  # folder names are UTC
        except ValueError:
            pass
        on_youtube = bool(meta.get("youtube_id")) and meta.get("privacy") != "deleted"
        age_days = (now - float(meta.get("uploaded_at") or made)) / 86400
        freed = 0
        if on_youtube and cfg["clean_after_upload"]:
            freed += remove_intermediates(d, dry_run)
        video = d / "short.mp4"
        drop_video = video.exists() and (
            (on_youtube and cfg["keep_video_days"] is not None and age_days >= float(cfg["keep_video_days"])) or
            (not on_youtube and cfg["keep_unuploaded_days"] is not None and age_days >= float(cfg["keep_unuploaded_days"])))
        if drop_video:
            if not on_youtube:
                freed += remove_intermediates(d, dry_run)
            size = video.stat().st_size
            if not dry_run:
                video.unlink()
                if meta:
                    meta["video_removed"] = now
                    save_json(d / "meta.json", meta)
            stats["videos_removed"] += 1
            stats["video_bytes"] += size
        if freed or drop_video:
            stats["folders"] += 1
        stats["pieces_bytes"] += freed
    total = (stats["pieces_bytes"] + stats["video_bytes"]) / GB
    if stats["folders"]:
        log.info("output cleanup%s: %d folders, %d video files removed, %.2f GB freed", " (dry run)" if dry_run else "",
                 stats["folders"], stats["videos_removed"], total)
    stats["freed_gb"] = round(total, 2)
    return stats