"""Channel health watch: a daily look at the channel's numbers for anything that breaks pattern.

Checked at every channel sync from the stats already fetched (no extra quota):
  - uploads that YouTube rejected, failed to process, age-restricted or that disappeared since the last sync;
  - the newest Shorts earning far fewer views per hour than the channel did over the previous two weeks (the feed
    stopped pushing, a strike, a change in the videos);
  - a collapse in likes per view on the newest Shorts (viewers reacting differently to what changed).
Each finding goes to Telegram once a day with the likely cause, and sits on the channel panel until it clears.
"""
from __future__ import annotations

import logging
import statistics as st
import time
from typing import Any

from .config import DATA_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "health.json"
DEFAULTS = {"enabled": True, "recent_hours": 72, "settle_hours": 6, "min_recent": 3, "min_before": 5,
            "drop_factor": 0.3, "min_vph": 0.5, "like_drop_factor": 0.3}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("schedule") or {}).get("health")) or {})
    return cfg


def _state() -> dict[str, Any]:
    return load_json(STATE_PATH) or {"known_ids": [], "findings": [], "checked_at": 0.0}


def check(videos: list[dict[str, Any]], known_ids: list[str] | None = None, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Findings from the channel's video stats: [{key, title, text}]. Pure: no files, no network."""
    cfg = cfg or config()
    out: list[dict[str, Any]] = []
    ids = {v["id"] for v in videos if v.get("id")}
    for vid in known_ids or []:
        if vid not in ids:
            out.append({"key": f"gone:{vid}", "title": "A video disappeared from the channel",
                        "text": f"Video {vid} is no longer listed on the channel. If you did not delete it, check YouTube Studio "
                                "for a removal notice or a strike."})
    for v in videos:
        why = None
        if (v.get("upload_status") or "") in ("rejected", "failed"):
            why = f"YouTube {v['upload_status']} it" + (f" ({v.get('rejection_reason')})" if v.get("rejection_reason") else "")
        elif v.get("age_restricted"):
            why = "it is age-restricted, so it is out of the Shorts feed for most viewers"
        if why:
            out.append({"key": f"video:{v['id']}", "title": "An upload has a problem",
                        "text": f"{v.get('title', v['id'])[:70]!r}: {why}. Open it in YouTube Studio; if the restriction is wrong, appeal there."})
    public = [v for v in videos if v.get("privacy", "public") == "public" and float(v.get("public_hours") or 0) >= float(cfg["settle_hours"])]
    recent = [v for v in public if float(v.get("age_hours") or 0) <= float(cfg["recent_hours"])]
    before = [v for v in public if float(cfg["recent_hours"]) < float(v.get("age_hours") or 0) <= float(cfg["recent_hours"]) + 14 * 24]
    if len(recent) >= int(cfg["min_recent"]) and len(before) >= int(cfg["min_before"]):
        r_vph = st.median(float(v.get("views_per_hour") or 0) for v in recent)
        b_vph = st.median(float(v.get("views_per_hour") or 0) for v in before)
        if b_vph >= float(cfg["min_vph"]) and r_vph <= float(cfg["drop_factor"]) * b_vph:
            out.append({"key": "views_drop", "title": "The newest Shorts get far fewer views than usual",
                        "text": f"The last {len(recent)} Shorts earn {r_vph:.1f} views/hour (median) against {b_vph:.1f} over the two weeks "
                                "before. Likely causes, most common first: YouTube stopped pushing the channel in the Shorts feed for "
                                "a while (usually passes; keep the schedule steady), a strike or restriction on the channel (check "
                                "YouTube Studio), or the newest videos changed in a way regulars skip (compare their post-mortems)."})

        def like_ratio(rows: list[dict[str, Any]]) -> float | None:
            views = sum(int(v.get("views") or 0) for v in rows)
            return (sum(int(v.get("likes") or 0) for v in rows) / views) if views >= 200 else None
        r_like, b_like = like_ratio(recent), like_ratio(before)
        if r_like is not None and b_like and r_like <= float(cfg["like_drop_factor"]) * b_like:
            out.append({"key": "likes_drop", "title": "Viewers like the newest Shorts much less",
                        "text": f"Likes per view fell to {r_like:.1%} on the last {len(recent)} Shorts from {b_like:.1%} before. Something "
                                "in the videos changed for the worse (voice, captions, subjects, length): compare the newest cards "
                                "with the winners."})
    return out


def sync(videos: list[dict[str, Any]] | None = None) -> str:
    """Channel-sync hook: check, alert once a day per finding, remember the findings. Returns a status line."""
    cfg = config()
    if not cfg["enabled"]:
        return "health: off"
    if videos is None:
        videos = (load_json(DATA_DIR / "channel_stats.json") or {}).get("videos") or []
    state = _state()
    findings = check(videos, state.get("known_ids") or [], cfg)
    try:
        from .alerts import send
        for f in findings:
            send(f"health:{f['key']}", f"🩺 {f['title']}\n{f['text']}", repeat_hours=24)
    except Exception as exc:  # noqa: BLE001
        log.debug("health alert not sent: %s", exc)
    state.update(known_ids=sorted({v["id"] for v in videos if v.get("id")}), findings=findings, checked_at=time.time())
    save_json(STATE_PATH, state)
    return "health: " + (f"{len(findings)} finding(s): " + "; ".join(f["title"] for f in findings) if findings else "nothing unusual")


def overview() -> dict[str, Any]:
    state = _state()
    return {"checked_at": state.get("checked_at") or None, "findings": state.get("findings") or []}


def report_lines() -> list[str]:
    f = overview()["findings"]
    return [f"Health: {len(f)} finding(s)"] + [f"  {x['title']}" for x in f[:3]] if f else []
