"""Auto playlists: every topic series gets a playlist in part order, weekly compilations get their own.

A playlist is the binge path on the channel page and in the Shorts player's "more from this series". After each
upload the Short is added to its series' playlist (created on first use, 50 quota units each for the playlist and
for every item). Parts uploaded before this existed are back-filled a few at a time, in part order. Playlist ids are
remembered in data/playlists.json; a playlist deleted on YouTube is created again.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "playlists.json"
DEFAULTS = {"enabled": True, "privacy": "public", "backfill_per_upload": 6, "compilations": "Weekly compilations"}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("upload") or {}).get("playlists")) or {})
    return cfg


def _state() -> dict[str, Any]:
    return load_json(STATE_PATH) or {"playlists": {}}


def title_for(meta: dict[str, Any]) -> str | None:
    """The playlist a finished video belongs to, or None."""
    if meta.get("mode") == "compilation" or meta.get("compilation"):
        return config()["compilations"]
    from .series import series_of
    name = series_of(meta)
    if not name:
        return None
    return re.sub(r"\s+", " ", name.strip()).title()[:150]


def _service():
    from .upload import youtube_service
    return youtube_service(interactive=False)


def ensure(title: str, yt=None) -> str:
    """The playlist id for `title`, created when missing."""
    from .upload import with_retries
    from .usage import record
    st = _state()
    pid = (st["playlists"].get(title) or {}).get("id")
    if pid:
        return pid
    yt = yt or _service()
    resp = with_retries(lambda: yt.playlists().insert(part="snippet,status", body={
        "snippet": {"title": title, "description": f"{title}: every part, in order."},
        "status": {"privacyStatus": config()["privacy"]}}).execute(), "create a playlist")
    record(youtube_units=50)
    st["playlists"][title] = {"id": resp["id"], "items": []}
    save_json(STATE_PATH, st)
    log.info("playlist created: %r (%s)", title, resp["id"])
    return resp["id"]


def _forget(title: str) -> None:
    st = _state()
    st["playlists"].pop(title, None)
    save_json(STATE_PATH, st)


def add(video_id: str, title: str, yt=None, _retry: bool = True) -> bool:
    """Append a video to the playlist (at the end: upload order). False when it is already there."""
    from googleapiclient.errors import HttpError
    from .upload import with_retries
    from .usage import record
    st = _state()
    if video_id in ((st["playlists"].get(title) or {}).get("items") or []):
        return False
    yt = yt or _service()
    pid = ensure(title, yt)
    try:
        with_retries(lambda: yt.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute(), "add to a playlist")
    except HttpError as exc:
        content = getattr(exc, "content", b"") or b""
        detail = (content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)) + str(exc)
        if _retry and "playlistNotFound" in detail:
            _forget(title)                                   # deleted on YouTube: make it again once
            return add(video_id, title, yt, _retry=False)
        raise
    record(youtube_units=50)
    st = _state()
    st["playlists"].setdefault(title, {"id": pid, "items": []})["items"].append(video_id)
    save_json(STATE_PATH, st)
    return True


def after_upload(out_dir: Path, meta: dict[str, Any], yt=None, output_dir: Path | None = None) -> dict[str, Any] | None:
    """Add the new upload to its playlist, then back-fill a few earlier parts that are not in it yet. Never raises.
    Returns {title, added, backfilled} or None."""
    cfg = config()
    if not cfg["enabled"] or not meta.get("youtube_id"):
        return None
    title = title_for(meta)
    if not title:
        return None
    try:
        yt = yt or _service()
        added = add(meta["youtube_id"], title, yt)
        done = 0
        if title != cfg["compilations"]:
            from .series import parts, series_of
            have = set(((_state()["playlists"].get(title) or {}).get("items") or []))
            for p in parts(series_of(meta), output_dir or OUTPUT_DIR, exclude=None):
                if done >= int(cfg["backfill_per_upload"]):
                    break
                if p["youtube_id"] not in have and p["privacy"] != "deleted":
                    if add(p["youtube_id"], title, yt):
                        done += 1
    except Exception as exc:  # noqa: BLE001 - a playlist must never fail an upload
        log.warning("playlist %r skipped: %s", title, str(exc)[:160])
        return None
    m = load_json(out_dir / "meta.json") or meta
    m["playlist"] = title
    save_json(out_dir / "meta.json", m)
    log.info("playlist %r: %s%s", title, "added" if added else "already in it", f", {done} earlier part(s) back-filled" if done else "")
    return {"title": title, "added": added, "backfilled": done}
