"""Where a Short's folder lives: output/ while it is new or needs you, archive/ once it is settled on YouTube.

The console lists output/ only, so the page shrinks as Shorts settle. An archived Short keeps just its details and
script (meta.json, script.json, a few KB) because the learning still reads them: retention and post-mortems, voice
casting, publish times, series links, the no-repeat history. The archive sits next to output/ (channels/<name>/archive
for a second channel), so code and tests that point output somewhere else get a matching archive automatically.
"""
from __future__ import annotations

from pathlib import Path


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


def find(name: str, output_dir: Path) -> Path:
    """A Short's folder by name, wherever it is now (output/ when it exists in neither)."""
    here = output_dir / name
    if here.exists():
        return here
    old = archive_dir(output_dir) / name
    return old if old.exists() else here
