"""Where a Short's folder lives: output/ while it is new or needs you, archive/ once it is settled on YouTube.

The console lists output/ only, so the page shrinks as Shorts settle. An archived Short keeps just its details and
script (meta.json, script.json, a few KB) because the learning still reads them: retention and post-mortems, voice
casting, publish times, series links, the no-repeat history. The archive sits next to output/ (channels/<name>/archive
for a second channel), so code and tests that point output somewhere else get a matching archive automatically.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any


def archive_dir(output_dir: Path) -> Path:
    return output_dir.parent / "archive"


def short_dirs(output_dir: Path, archived: bool = True) -> list[Path]:
    """Every Short folder: output/ first, then the archive. A name in both counts once (the output/ copy)."""
    out: list[Path] = []
    seen: set[str] = set()
    roots = [output_dir] + ([archive_dir(output_dir)] if archived else [])
    for root in roots:
        if not root.exists():
            continue
        for d in root.iterdir():
            if d.is_dir() and d.name not in seen:
                seen.add(d.name)
                out.append(d)
    return out


def live_privacy(meta: dict[str, Any], now: float | None = None) -> str | None:
    """The privacy a Short has on YouTube right now, as far as we know. YouTube publishes a scheduled Short by itself,
    so a private Short whose publish time has passed is public, even before the next privacy refresh writes it down."""
    privacy = meta.get("privacy")
    at = meta.get("publish_at")
    if privacy == "private" and at and meta.get("youtube_id"):
        try:
            when = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return privacy
        if when <= (now or time.time()):
            return "public"
    return privacy


def find(name: str, output_dir: Path) -> Path:
    """A Short's folder by name, wherever it is now (output/ when it exists in neither)."""
    here = output_dir / name
    if here.exists():
        return here
    old = archive_dir(output_dir) / name
    return old if old.exists() else here
