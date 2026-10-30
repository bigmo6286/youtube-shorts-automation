"""Your own footage library: clips and images you drop in a folder become the first footage source.

Folders: assets/footage (shared by every channel on this machine) and, for a second channel, channels/<name>/assets/
footage. Each file gets a one-line description from library.json in that folder (edit it in Settings -> Your own
footage), from a .txt next to it, or from its file name ("lion_drinking_at_waterhole.mp4" -> "lion drinking at
waterhole"). For each script line the best-matching library files are offered to TypeSafe before any stock site,
judged with the same questions and the same image check, and a used file is not reused in the same Short.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from .config import HOME, ROOT
from .storage import load_json, save_json

log = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_OFFERED = 12                # library files offered to TypeSafe per line (the best word matches)
_STOP = {"the", "a", "an", "of", "in", "on", "at", "and", "or", "to", "with", "for", "from", "by", "is", "are", "was"}


def folders() -> list[Path]:
    out = [ROOT / "assets" / "footage"]
    if HOME != ROOT:
        out.append(HOME / "assets" / "footage")
    return out


def _from_name(path: Path) -> str:
    return re.sub(r"[\s_\-]+", " ", re.sub(r"\d{3,}", " ", path.stem)).strip() or path.stem


def items() -> list[dict[str, Any]]:
    """Every library file with its description."""
    out = []
    for folder in folders():
        if not folder.exists():
            continue
        descriptions = load_json(folder / "library.json") or {}
        for p in sorted(folder.iterdir()):
            ext = p.suffix.lower()
            if ext not in VIDEO_EXT | IMAGE_EXT or p.name.startswith("."):
                continue
            desc = (descriptions.get(p.name) or "").strip()
            if not desc and (p.with_suffix(".txt")).exists():
                desc = p.with_suffix(".txt").read_text(encoding="utf-8", errors="replace").strip().splitlines()[0][:200]
            out.append({"id": "lib" + hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:12],
                        "kind": "video" if ext in VIDEO_EXT else "photo", "description": desc or _from_name(p),
                        "described": bool(desc), "local": str(p), "name": p.name, "folder": str(folder),
                        "source": "library", "preview": ""})
    return out


def set_descriptions(updates: dict[str, str]) -> int:
    """Save descriptions ({file name: text}) into the right folder's library.json."""
    n = 0
    for folder in folders():
        if not folder.exists():
            continue
        names = {p.name for p in folder.iterdir()}
        mine = {k: v.strip() for k, v in updates.items() if k in names}
        if not mine:
            continue
        current = load_json(folder / "library.json") or {}
        current.update(mine)
        save_json(folder / "library.json", {k: v for k, v in current.items() if v})
        n += len(mine)
    return n


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP and len(w) > 2}


def candidates(keyword: str, line_text: str, limit: int = MAX_OFFERED) -> list[dict[str, Any]]:
    """The library files most likely to fit this line (word overlap with the footage term first, then the line)."""
    all_items = items()
    if len(all_items) <= limit:
        return all_items
    kw, line = _words(keyword), _words(line_text)
    def score(it: dict[str, Any]) -> float:
        d = _words(it["description"])
        return 2 * len(d & kw) + len(d & line)
    ranked = sorted(all_items, key=score, reverse=True)
    return ranked[:limit]
