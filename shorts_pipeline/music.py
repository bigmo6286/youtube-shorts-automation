"""Background music library: your own files plus free Creative-Commons tracks fetched from Openverse.

Tracks live in assets/music/. index.json keeps title, creator, licence and source for each fetched file
so a CC-BY track gets its attribution line appended to the video description automatically.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any

import requests

from .config import ASSETS_DIR

log = logging.getLogger(__name__)

MUSIC_DIR = ASSETS_DIR / "music"
INDEX_PATH = MUSIC_DIR / "index.json"
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".ogg", ".oga", ".flac", ".aac")
OPENVERSE = "https://api.openverse.org/v1/audio/"
# Only licences that allow commercial use and modification (YouTube monetisation, our remix into a video).
ALLOWED_LICENSES = {"cc0", "by", "by-sa", "pdm"}
USER_AGENT = "youtube-shorts-automation/1.0 (background music fetch)"


def _load_index() -> dict[str, dict[str, Any]]:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_index(index: dict[str, dict[str, Any]]) -> None:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def list_tracks() -> list[dict[str, Any]]:
    """Every audio file in assets/music with whatever metadata we know about it."""
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_index()
    tracks = []
    for p in sorted(MUSIC_DIR.iterdir()):
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        meta = index.get(p.name, {})
        tracks.append({
            "file": p.name, "path": str(p), "size": p.stat().st_size,
            "title": meta.get("title") or p.stem, "creator": meta.get("creator", ""),
            "license": meta.get("license", "own"), "source": meta.get("source", "uploaded"),
            "source_url": meta.get("source_url", ""), "duration": meta.get("duration"),
        })
    return tracks


def pick_track(choice: str | None) -> dict[str, Any] | None:
    """'none' -> no music; 'random' -> any track; otherwise a file name or title substring."""
    choice = (choice or "random").strip()
    if choice.lower() in ("none", "off", "no", ""):
        return None
    tracks = list_tracks()
    if not tracks:
        return None
    if choice.lower() == "random":
        return random.choice(tracks)
    low = choice.lower()
    for t in tracks:
        if t["file"].lower() == low or t["file"].lower() == low + ".mp3":
            return t
    for t in tracks:
        if low in t["file"].lower() or low in t["title"].lower():
            return t
    log.warning("no music track matches %r; using a random one", choice)
    return random.choice(tracks)


def attribution_line(track: dict[str, Any] | None) -> str:
    """Credit line required by CC BY / BY-SA. CC0 and your own files need none."""
    if not track or track.get("license", "own") in ("own", "cc0", "pdm"):
        return ""
    lic = track["license"].upper().replace("BY", "BY")
    creator = track.get("creator") or "unknown artist"
    line = f"Music: {track['title']} by {creator} (CC {lic})"
    if track.get("source_url"):
        line += f" {track['source_url']}"
    return line


def delete_track(file_name: str) -> bool:
    p = MUSIC_DIR / Path(file_name).name
    if not p.exists():
        return False
    p.unlink()
    index = _load_index()
    if p.name in index:
        del index[p.name]
        _save_index(index)
    return True


def _safe_name(title: str, ext: str) -> str:
    base = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:60] or "track"
    return f"{base}{ext}"


def fetch_tracks(query: str, count: int = 5, *, min_seconds: float = 15, max_seconds: float = 420) -> list[dict[str, Any]]:
    """Search Openverse for CC0 / CC-BY music and download up to `count` new tracks."""
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_index()
    have_urls = {m.get("source_url") for m in index.values()}
    # Anonymous Openverse access allows 20 results per page; two pages is plenty for a handful of tracks.
    results: list[dict[str, Any]] = []
    log.info("searching Openverse for %r ...", query)
    for page in (1, 2):
        params = {"q": query, "license": "cc0,by,by-sa,pdm", "page_size": 20, "page": page}
        r = requests.get(OPENVERSE, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
        if r.status_code == 429:
            raise RuntimeError("Openverse rate limit reached (anonymous use is limited); try again in an hour")
        r.raise_for_status()
        batch = r.json().get("results", [])
        results.extend(batch)
        if len(batch) < 20:
            break
    log.info("%d candidates", len(results))
    added: list[dict[str, Any]] = []
    for item in results:
        if len(added) >= count:
            break
        lic = (item.get("license") or "").lower()
        if lic not in ALLOWED_LICENSES:
            continue
        seconds = (item.get("duration") or 0) / 1000.0
        if not (min_seconds <= seconds <= max_seconds):
            continue
        url = item.get("url") or ""
        if not url or url in have_urls:
            continue
        ftype = (item.get("filetype") or "").lower()
        ext = ".mp3" if ftype in ("mp3", "mp32", "") else ".ogg" if ftype in ("ogg", "oga", "ogx") else ".mp3"
        name = _safe_name(item.get("title") or "track", ext)
        dest = MUSIC_DIR / name
        if dest.exists():
            dest = MUSIC_DIR / _safe_name(f"{item.get('title') or 'track'}_{item.get('id', '')[:6]}", ext)
        try:
            with requests.get(url, stream=True, timeout=90, headers={"User-Agent": USER_AGENT}) as dl:
                dl.raise_for_status()
                ctype = dl.headers.get("content-type", "")
                if "audio" not in ctype and "octet-stream" not in ctype:
                    log.info("skip %s: content-type %s", name, ctype)
                    continue
                with open(dest, "wb") as f:
                    for chunk in dl.iter_content(1 << 16):
                        f.write(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed for %s: %s", name, exc)
            dest.unlink(missing_ok=True)
            continue
        meta = {"title": item.get("title") or dest.stem, "creator": item.get("creator") or "",
                "license": lic, "source": item.get("source") or "openverse",
                "source_url": item.get("foreign_landing_url") or url, "duration": round(seconds, 1), "query": query}
        index[dest.name] = meta
        have_urls.add(url)
        added.append({"file": dest.name, **meta})
        log.info("added %s (%s, %.0fs, %s)", dest.name, lic, seconds, meta["creator"] or "unknown")
    _save_index(index)
    if not added:
        log.warning("no new tracks found for %r (try another word: lofi, ambient, upbeat, piano, cinematic)", query)
    return added
