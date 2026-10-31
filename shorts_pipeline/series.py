"""Series navigation: every uploaded Short of a topic series points at the part before it, the first part, and
(once it exists) the next part.

When a series Short is uploaded, its description gets "Part N of the <series> series", a link to the previous part
and a link to part 1. The previous part's description gets "Next part: <link>" (one videos.update, 50 quota units),
and once the videos are public each gets a comment with the same links (50 units each), so a viewer who lands on
part 3 can go back to the start and a viewer of part 2 learns part 3 is out. Part numbers are given in upload order
and never change. Everything that could not be done at once (a part still private, YouTube busy) stays pending in
the Short's meta.json and is retried by the comments pass.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .config import OUTPUT_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "comments": True, "update_previous": True}
MAX_ATTEMPTS = 5


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("series_links")) or {})
    return cfg


def url(video_id: str) -> str:
    return f"https://youtube.com/shorts/{video_id}"


def series_of(meta: dict[str, Any] | None) -> str | None:
    """The series name of a topic-series Short ("bible stories"), else None."""
    bp = (meta or {}).get("blueprint") or {}
    if str(bp.get("key") or "").startswith("series:") and bp.get("series"):
        return str(bp["series"]).strip()
    return (meta or {}).get("series_name") or None


def parts(series: str, output_dir: Path | None = None, exclude: str | None = None) -> list[dict[str, Any]]:
    """The uploaded parts of a series in part order: {dir, youtube_id, title, part, privacy}. Parts uploaded before
    numbering existed are numbered by upload order (and remembered) the first time they are seen."""
    output_dir = output_dir or OUTPUT_DIR
    found: list[tuple[Path, dict[str, Any]]] = []
    if not output_dir.exists():
        return []
    for d in output_dir.iterdir():
        if not d.is_dir() or d.name == exclude:
            continue
        meta = load_json(d / "meta.json")
        if not meta or series_of(meta) != series or not meta.get("youtube_id") or meta.get("privacy") == "deleted":
            continue
        found.append((d, meta))
    numbered = [(d, m) for d, m in found if m.get("series_part")]
    unnumbered = sorted([(d, m) for d, m in found if not m.get("series_part")],
                        key=lambda dm: (float(dm[1].get("uploaded_at") or 0), dm[0].name))
    next_no = max([int(m["series_part"]) for _, m in numbered], default=0) + 1
    for d, m in unnumbered:
        m["series_part"], m["series_name"] = next_no, series
        save_json(d / "meta.json", m)
        next_no += 1
    rows = [{"dir": d.name, "youtube_id": m["youtube_id"], "title": m.get("title", ""), "part": int(m["series_part"]),
             "privacy": m.get("privacy")} for d, m in numbered + unnumbered]
    rows.sort(key=lambda r: r["part"])
    return rows


def prepare(out_dir: Path, meta: dict[str, Any]) -> dict[str, Any] | None:
    """Before the upload: this Short's part number and the lines to add to its description, or None when it is not
    part of a series (or the feature is off)."""
    series = series_of(meta)
    if not series or not config()["enabled"]:
        return None
    previous = parts(series, exclude=out_dir.name)
    n = (previous[-1]["part"] + 1) if previous else 1
    lines = [f"Part {n} of the {series} series."]
    if previous:
        lines.append(f"Previous part: {url(previous[-1]['youtube_id'])}")
        if len(previous) > 1:
            lines.append(f"Start from part 1: {url(previous[0]['youtube_id'])}")
    return {"series": series, "part": n, "previous": previous[-1] if previous else None,
            "first": previous[0] if previous else None, "block": "\n".join(lines)}


def link_after_upload(out_dir: Path, meta: dict[str, Any], info: dict[str, Any]) -> list[str]:
    """Record the part number and queue the links: a 'Next part' line on the previous part's description, and the
    comments. Applies what it can right away. Returns what was done (for the log)."""
    cfg = config()
    meta = load_json(out_dir / "meta.json") or meta
    meta["series_part"], meta["series_name"] = info["part"], info["series"]
    pending: list[dict[str, Any]] = []
    vid = meta["youtube_id"]
    prev = info.get("previous")
    if cfg["comments"]:
        text = f"Part {info['part']} of the {info['series']} series."
        if prev:
            text += f"\nPrevious part: {url(prev['youtube_id'])}"
            if info.get("first") and info["first"]["youtube_id"] != prev["youtube_id"]:
                text += f"\nStart from part 1: {url(info['first']['youtube_id'])}"
        pending.append({"type": "comment", "video": vid, "dir": out_dir.name, "text": text, "attempts": 0})
    if prev:
        if cfg["update_previous"]:
            pending.append({"type": "description", "video": prev["youtube_id"], "dir": prev["dir"],
                            "append": f"Next part: {url(vid)}", "attempts": 0})
        if cfg["comments"]:
            pending.append({"type": "comment", "video": prev["youtube_id"], "dir": prev["dir"],
                            "text": f"Part {info['part']} is out: {url(vid)}", "attempts": 0})
    meta["series_pending"] = pending
    save_json(out_dir / "meta.json", meta)
    done = flush(only=out_dir.name)
    return done


def _comments_service():
    from . import comments
    if not comments.connected():
        return None
    try:
        return comments._service(interactive=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("series comments: no comments service (%s)", str(exc)[:120])
        return None


def _append_description(video_id: str, append: str, local_dir: Path | None) -> bool:
    """Add a line to a video's description on YouTube (and in its local meta). False when it was already there."""
    from .upload import with_retries, youtube_service
    yt = with_retries(lambda: youtube_service(interactive=False), "connect to YouTube")
    r = with_retries(lambda: yt.videos().list(part="snippet", id=video_id).execute(), "read video description")
    items = r.get("items") or []
    if not items:
        raise RuntimeError(f"video {video_id} not found")
    sn = items[0]["snippet"]
    if append in (sn.get("description") or ""):
        return False
    desc = ((sn.get("description") or "").rstrip() + "\n\n" + append)[:5000]
    body = {"id": video_id, "snippet": {"title": sn.get("title"), "description": desc, "categoryId": sn.get("categoryId"),
                                        "tags": sn.get("tags") or []}}
    for k in ("defaultLanguage", "defaultAudioLanguage"):
        if sn.get(k):
            body["snippet"][k] = sn[k]
    with_retries(lambda: yt.videos().update(part="snippet", body=body).execute(), "update video description")
    try:
        from .usage import record
        record(youtube_units=51)
    except Exception:  # noqa: BLE001
        pass
    if local_dir is not None:
        m = load_json(local_dir / "meta.json")
        if m and append not in (m.get("description") or ""):
            m["description"] = (m.get("description") or "").rstrip() + "\n\n" + append
            save_json(local_dir / "meta.json", m)
    return True


def _post_comment(yt, video_id: str, text: str) -> str:
    from .upload import with_retries
    body = {"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text[:500]}}}}
    r = with_retries(lambda: yt.commentThreads().insert(part="snippet", body=body).execute(), "post series comment")
    try:
        from .usage import record
        record(youtube_units=50)
    except Exception:  # noqa: BLE001
        pass
    return r.get("id", "")


def flush(yt=None, only: str | None = None, output_dir: Path | None = None) -> list[str]:
    """Apply pending series links. Description changes go at once; comments wait until the video is public (a comment
    on a private video is not seen) and need the comments connection. Returns a line per action done."""
    output_dir = output_dir or OUTPUT_DIR
    done: list[str] = []
    if not output_dir.exists():
        return done
    comments_yt = yt
    for d in sorted(output_dir.iterdir(), reverse=True):
        if not d.is_dir() or (only and d.name != only):
            continue
        meta = load_json(d / "meta.json")
        pending = (meta or {}).get("series_pending") or []
        if not pending:
            continue
        keep: list[dict[str, Any]] = []
        done_here: list[str] = []
        for act in pending:
            if act.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            target_meta = load_json(output_dir / act["dir"] / "meta.json") or {}
            try:
                if act["type"] == "description":
                    _append_description(act["video"], act["append"], output_dir / act["dir"])
                    done_here.append(f"description of {act['dir']}: {act['append']}")
                    continue
                if target_meta.get("privacy") != "public":
                    keep.append(act)                        # comment later, once the video is public
                    continue
                comments_yt = comments_yt or _comments_service()
                if comments_yt is None:
                    keep.append(act)
                    continue
                _post_comment(comments_yt, act["video"], act["text"])
                done_here.append(f"comment on {act['dir']}: {act['text'].splitlines()[0]}")
            except Exception as exc:  # noqa: BLE001
                act["attempts"] = int(act.get("attempts", 0)) + 1
                act["error"] = str(exc)[:160]
                keep.append(act)
                log.warning("series link on %s failed (%s)", act["dir"], str(exc)[:160])
        meta = load_json(d / "meta.json") or meta
        meta["series_pending"] = keep
        if done_here:
            meta["series_links_done"] = (meta.get("series_links_done") or []) + done_here
        save_json(d / "meta.json", meta)
        done.extend(done_here)
    for line in done:
        log.info("series: %s", line)
    return done
