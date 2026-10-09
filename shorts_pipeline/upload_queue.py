"""Automatic upload queue: scheduled Shorts wait here and the best ones are uploaded, within YouTube's limit.

- Every scheduled Short gets an upload priority (0-100) when it is produced: TypeSafe judges how likely it is to
  stop a scroller, how well it fits what already works on the channel, and whether viewers will watch to the end;
  the channel factor of its format x topic adds the measured evidence.
- The queue uploads the highest-priority waiting Short whenever the channel is under its daily limit (uploads in
  the last 24 hours), spacing uploads by `min_gap_minutes`. Shorts below `min_priority` are kept for a manual
  decision instead of being uploaded automatically, and Shorts waiting longer than `max_age_hours` expire (still
  uploadable from their card).
- When YouTube refuses an upload because the channel limit is reached, the limit is lowered to what YouTube
  actually allowed and the queue pauses until the oldest upload of the window is 24 hours old. A used-up API quota
  pauses it until the quota resets. A network failure is retried on the next turn, up to MAX_ATTEMPTS.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "upload_queue.json"
MAX_ATTEMPTS = 3
LEARNED_LIMIT_DAYS = 3        # a limit learned from YouTube's refusal is trusted this long (channel limits grow)
_LOCK = threading.Lock()

DEFAULTS = {
    "auto": True,             # scheduled Shorts are uploaded automatically through this queue
    "daily_limit": 20,        # uploads per rolling 24 hours; lowered automatically when YouTube refuses
    "min_priority": 30,       # Shorts predicted below this (0-100) wait for you instead of uploading
    "max_age_hours": 36,      # waiting longer than this: not uploaded automatically (trend has moved on)
    "min_gap_minutes": 20,    # spacing between automatic uploads
}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (load_config().get("upload") or {}).items() if k in DEFAULTS})
    return cfg


def _state() -> dict[str, Any]:
    state = load_json(STATE_PATH)
    if state is None:                      # first run: count the uploads already made, from the job logs
        state = {"uploads": _uploads_from_logs()}
        _save(state)
    return state


def _uploads_from_logs(days: float = 2) -> list[float]:
    """Times of successful uploads in the last `days`, read from the console's job logs (manual and produce --upload)."""
    log_dir = DATA_DIR / "logs"
    out: list[float] = []
    if not log_dir.exists():
        return out
    cutoff = time.time() - days * 86400
    for p in log_dir.glob("*.log"):
        if "_upload_" not in p.name and "_produce_" not in p.name:
            continue
        try:
            started = datetime.strptime(p.name[:19], "%Y-%m-%dT%H-%M-%S").timestamp()
        except ValueError:
            continue
        if started < cutoff:
            continue
        try:
            if "Uploaded as" in p.read_text(encoding="utf-8", errors="replace"):
                out.append(max(started, p.stat().st_mtime))      # the log is written when the job ends
        except OSError:
            continue
    return sorted(out)


def _save(state: dict[str, Any]) -> None:
    state["uploads"] = sorted(state.get("uploads", []))[-300:]
    save_json(STATE_PATH, state)


# ---------------------------------------------------------------------------------------------------- limits

def uploads_24h(state: dict[str, Any] | None = None, now: float | None = None) -> list[float]:
    state = state or _state()
    now = now or time.time()
    return [t for t in state.get("uploads", []) if now - t < 24 * 3600]


def limit(state: dict[str, Any] | None = None) -> int:
    state = state or _state()
    cfg_limit = max(0, int(config()["daily_limit"]))
    try:
        from . import originality, tuning
        learned_volume = tuning.daily_upload_limit()
        if learned_volume:
            cfg_limit = min(cfg_limit, max(1, int(learned_volume * originality.volume_factor())))
    except Exception:  # noqa: BLE001 - no channel data yet: the configured limit
        pass
    learned = state.get("learned_limit")
    if learned and time.time() - float(state.get("learned_at") or 0) < LEARNED_LIMIT_DAYS * 86400:
        return min(cfg_limit, int(learned))
    return cfg_limit


def paused(state: dict[str, Any] | None = None) -> float | None:
    state = state or _state()
    until = state.get("paused_until")
    return float(until) if until and float(until) > time.time() else None


def record_upload(out_dir: Path | None = None) -> None:
    with _LOCK:
        state = _state()
        state.setdefault("uploads", []).append(time.time())
        if paused(state) is None:
            state.pop("paused_until", None)
            state.pop("paused_reason", None)
        _save(state)


def _next_pacific_midnight() -> float:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        now = datetime.now(tz)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return nxt.timestamp()
    except Exception:  # noqa: BLE001 - no tz database: assume UTC-7
        now = datetime.now(timezone.utc) - timedelta(hours=7)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return (nxt + timedelta(hours=7)).timestamp()


def on_upload_error(out_dir: Path, error: str) -> str:
    """Update the queue after a failed upload. Returns a note for the owner (used in the alert)."""
    from .alerts import classify

    problem = classify(error)
    note = ""
    with _LOCK:
        state = _state()
        if problem.key == "upload_limit":
            window = uploads_24h(state)
            state["learned_limit"] = max(1, len(window))
            state["learned_at"] = time.time()
            until = (min(window) + 24 * 3600 + 300) if window else time.time() + 12 * 3600
            state["paused_until"] = until
            state["paused_reason"] = f"YouTube allowed {len(window)} uploads in 24 hours"
            note = f"Uploads paused until {datetime.fromtimestamp(until).strftime('%a %H:%M')}; the daily limit is now {state['learned_limit']}."
        elif problem.key == "api_quota":
            until = _next_pacific_midnight()
            state["paused_until"] = until
            state["paused_reason"] = "YouTube API quota used up"
            note = f"Uploads paused until {datetime.fromtimestamp(until).strftime('%a %H:%M')} (quota reset)."
        _save(state)
    meta_path = out_dir / "meta.json"
    meta = load_json(meta_path)
    if meta and meta.get("upload_state") == "queued" and problem.key not in ("upload_limit", "api_quota", "youtube_token"):
        meta["upload_attempts"] = int(meta.get("upload_attempts") or 0) + 1
        meta["upload_error"] = error[:300]
        if meta["upload_attempts"] >= MAX_ATTEMPTS:
            meta["upload_state"] = "failed"
            note = note or f"Gave up after {MAX_ATTEMPTS} attempts; upload it from its card."
        else:
            note = note or f"It stays in the queue and is retried (attempt {meta['upload_attempts']} of {MAX_ATTEMPTS})."
        save_json(meta_path, meta)
    return note


# ---------------------------------------------------------------------------------------------------- priority

PRIORITY_QUESTIONS: dict[str, Any] | None = None


def _questions() -> dict[str, Any]:
    from .judge import _q
    return {
        "scroll_stop": _q("score",
            "A viewer is swiping through YouTube Shorts and sees `video.title` while hearing `video.hook`. How likely "
            "are they to stop and keep watching instead of swiping away?",
            ["Almost certainly swipes away: generic, vague or uninteresting",
             "Unlikely to stop: mildly interesting but easy to skip",
             "Might stop: a decent hook with some curiosity or surprise",
             "Likely to stop: specific, surprising or emotionally gripping",
             "Very likely to stop: an irresistible, concrete hook that demands the answer"]),
        "channel_fit": _q("score",
            "How well does `video` match what already performs on this channel? Compare its subject, format and tone "
            "with `channel.best_recent_videos` (high views per hour) and `channel.weak_recent_videos` (low).",
            ["Resembles the channel's weakest videos", "Closer to the weak videos than the strong ones",
             "Neither clearly like the strong nor the weak videos",
             "Similar in kind to the channel's strong videos",
             "Very close to the channel's best performers in subject, format and tone"]),
        "watch_through": _q("score",
            "Reading `video.script`, how likely is a viewer who started watching to stay to the end (clear payoff, "
            "no filler, tension kept until the last line)?",
            ["Most viewers leave early: rambling, no payoff", "Many leave: slow or weak payoff",
             "About average for Shorts", "Most stay: tight with a satisfying payoff",
             "Nearly everyone stays: gripping throughout with a strong ending"]),
    }


def _channel_context() -> dict[str, Any]:
    data = load_json(DATA_DIR / "channel_stats.json") or {}
    vids = [v for v in data.get("videos", []) if float(v.get("age_hours") or 0) >= 24]
    vids.sort(key=lambda v: -float(v.get("views_per_hour") or 0))
    pick = lambda vs: [{"title": v["title"], "views_per_hour": v.get("views_per_hour")} for v in vs]  # noqa: E731
    return {"best_recent_videos": pick(vids[:6]), "weak_recent_videos": pick(vids[-6:]) if len(vids) > 12 else []}


def score_short(out_dir: Path, meta: dict[str, Any] | None = None, script: dict[str, Any] | None = None) -> dict[str, Any]:
    """Priority 0-100 for the upload queue. TypeSafe judgments (85%) + the channel factor of the blueprint (15%).
    Without TypeSafe: the script QA hook score and the channel factor."""
    from . import judge

    meta = meta or load_json(out_dir / "meta.json") or {}
    script = script or load_json(out_dir / "script.json") or {}
    bp = meta.get("blueprint") or {}
    if bp.get("source") == "channel":
        factor = float(bp.get("opportunity") or 1.0)                 # channel winners carry their factor
    else:
        try:
            from .channel import blueprint_performance, factor_for
            factor = float(factor_for(blueprint_performance(), bp.get("format", ""), bp.get("topic", ""))[0])
        except Exception:  # noqa: BLE001 - no channel data yet: neutral
            factor = 1.0
    factor_part = max(0.0, min(1.0, factor / 3.0))                 # x3 the channel median or better counts as full
    parts: dict[str, float] = {"channel_factor": round(factor_part, 3)}
    if judge.has_typesafe():
        state = {"video": {"title": meta.get("title", ""), "hook": script.get("hook", ""),
                           "script": (script.get("full_text") or "")[:1500],
                           "format": bp.get("format"), "topic": bp.get("topic")},
                 "channel": _channel_context()}
        try:
            with judge._client() as client:
                r = client.system_one(state=state, questions=_questions())
            for k in ("scroll_stop", "channel_fit", "watch_through"):
                parts[k] = round(float(r.answers[k].score) / 4, 3)
        except Exception as exc:  # noqa: BLE001
            log.warning("upload priority: TypeSafe unavailable (%s); using the script QA score", str(exc)[:120])
    if "scroll_stop" in parts:
        value = 0.35 * parts["scroll_stop"] + 0.25 * parts["channel_fit"] + 0.25 * parts["watch_through"] + 0.15 * factor_part
    else:
        hook = float(((script.get("qa") or {}).get("hook_strength") or {}).get("score", 1.5)) / 3
        value = 0.7 * hook + 0.3 * factor_part
    priority = int(round(100 * value))
    # Experiments (untried formats) score low on "fits the channel" by definition, and sequels / viewer requests are
    # made on purpose: lift them so they get published and measured instead of expiring in the queue.
    if bp.get("explore") or bp.get("source") in ("sequel", "viewer_idea"):
        priority = min(100, max(priority + 10, int(config()["min_priority"]) + 15))
        parts["special_bonus"] = 1.0
    return {"priority": priority, "parts": parts}


def enqueue(out_dir: Path) -> dict[str, Any]:
    """Put a freshly produced Short in the queue with its priority."""
    meta = load_json(out_dir / "meta.json") or {}
    pr = score_short(out_dir, meta)
    meta = load_json(out_dir / "meta.json") or meta          # re-read: scoring took a moment
    meta.update({"upload_state": "queued", "queued_at": time.time(), "upload_priority": pr["priority"],
                 "priority_parts": pr["parts"]})
    save_json(out_dir / "meta.json", meta)
    rank, total = position(out_dir.name)
    log.info("upload queue: priority %d/100 (%s), #%d of %d waiting", pr["priority"],
             ", ".join(f"{k} {v:.2f}" for k, v in pr["parts"].items()), rank, total)
    return {"priority": pr["priority"], "rank": rank, "waiting": total}


# ---------------------------------------------------------------------------------------------------- queue

def queued() -> list[dict[str, Any]]:
    items = []
    if not OUTPUT_DIR.exists():
        return items
    for d in OUTPUT_DIR.iterdir():
        if not d.is_dir():
            continue
        meta = load_json(d / "meta.json")
        if meta and meta.get("upload_state") == "queued" and not meta.get("youtube_id") and (d / "short.mp4").exists():
            items.append({"dir": d.name, "path": d, "priority": int(meta.get("upload_priority") or 0),
                          "queued_at": float(meta.get("queued_at") or d.stat().st_mtime), "title": meta.get("title", "")})
    items.sort(key=lambda x: (-x["priority"], -x["queued_at"]))
    return items


def position(dir_name: str) -> tuple[int, int]:
    items = queued()
    for i, it in enumerate(items, 1):
        if it["dir"] == dir_name:
            return i, len(items)
    return 0, len(items)


def expire_old() -> int:
    max_age = float(config()["max_age_hours"]) * 3600
    n = 0
    for it in queued():
        if time.time() - it["queued_at"] > max_age:
            meta = load_json(it["path"] / "meta.json")
            if meta and meta.get("upload_state") == "queued":
                meta["upload_state"] = "expired"
                meta["expired_at"] = time.time()
                save_json(it["path"] / "meta.json", meta)
                log.info("upload queue: %r waited over %d h; not uploading it automatically", it["title"][:60],
                         config()["max_age_hours"])
                n += 1
    return n


def next_upload(now: float | None = None) -> tuple[dict[str, Any] | None, str]:
    """The Short to upload now, or None with the reason."""
    cfg = config()
    now = now or time.time()
    if not cfg["auto"]:
        return None, "automatic upload is off"
    state = _state()
    if paused(state):
        return None, f"paused until {datetime.fromtimestamp(paused(state)).strftime('%a %H:%M')} ({state.get('paused_reason', '')})"
    if len(uploads_24h(state, now)) >= limit(state):
        oldest = min(uploads_24h(state, now))
        return None, f"daily limit {limit(state)} reached; next slot {datetime.fromtimestamp(oldest + 86400).strftime('%H:%M')}"
    last = float(state.get("last_auto") or 0)
    if now - last < float(cfg["min_gap_minutes"]) * 60:
        return None, f"spacing uploads; next at {datetime.fromtimestamp(last + float(cfg['min_gap_minutes']) * 60).strftime('%H:%M')}"
    from .upload import TOKEN_PATH
    if not TOKEN_PATH.exists():
        return None, "YouTube is not signed in (upload once from a card to sign in)"
    for it in queued():
        if it["priority"] >= int(cfg["min_priority"]):
            return it, ""
    return None, "nothing waiting above the minimum priority"


def mark_started() -> None:
    with _LOCK:
        state = _state()
        state["last_auto"] = time.time()
        _save(state)


def summary() -> dict[str, Any]:
    state = _state()
    items = queued()
    cfg = config()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    expired_today = 0
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            m = load_json(d / "meta.json") if d.is_dir() else None
            if m and m.get("upload_state") == "expired" and float(m.get("expired_at") or 0) >= today:
                expired_today += 1
    nxt, reason = next_upload()
    p = paused(state)
    return {"auto": bool(cfg["auto"]), "queued": len(items), "limit": limit(state), "uploaded_24h": len(uploads_24h(state)),
            "min_priority": cfg["min_priority"], "below_min": sum(1 for i in items if i["priority"] < int(cfg["min_priority"])),
            "paused_until": datetime.fromtimestamp(p).strftime("%a %H:%M") if p else None,
            "paused_reason": state.get("paused_reason") if p else None,
            "learned_limit": state.get("learned_limit"), "expired_today": expired_today,
            "next": {"dir": nxt["dir"], "title": nxt["title"], "priority": nxt["priority"]} if nxt else None,
            "waiting_reason": reason,
            "top": [{"dir": i["dir"], "title": i["title"], "priority": i["priority"]} for i in items[:5]]}
