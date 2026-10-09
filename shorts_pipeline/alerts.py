"""Telegram alerts for failures, and a daily report.

`classify(error)` turns an error message into a known problem: a short title, the fix for the owner, and whether
the failure is worth retrying automatically. It is shared with the scheduler's slot retry and the upload queue.
`job_failed()` sends one alert per problem (the same problem is not repeated within ALERT_REPEAT_HOURS), and
`daily_report()` sends one summary a day: produced, uploaded, queued, failed, the best recent videos, disk space.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ROOT, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "alerts.json"
LOG_DIR = DATA_DIR / "logs"
ALERT_REPEAT_HOURS = 3
_LOCK = threading.Lock()


@dataclass
class Problem:
    key: str            # stable id, used to avoid repeating the same alert
    title: str
    fix: str
    retryable: bool     # worth an automatic retry later (network, a crashed render, a flaky voice service)


_RULES: list[tuple[str, Problem]] = [
    (r"invalid_grant|RefreshError|needs a new consent|not authorised yet",
     Problem("youtube_token", "YouTube sign-in expired",
             "Open the console and click Upload on any video card (or Sync channel now), then approve the Google page.",
             False)),
    (r"uploadLimitExceeded|exceeded the number of videos",
     Problem("upload_limit", "YouTube's daily upload limit for the channel is reached",
             "Nothing to do: the queue pauses and uploads the waiting Shorts automatically when the limit resets.",
             False)),
    (r"quotaExceeded|exceeded your quota|quota.*exceeded",
     Problem("api_quota", "YouTube API daily quota used up",
             "Nothing to do: it resets at midnight Pacific time and the queue continues then.", False)),
    (r"GB free on the disk|No space left|ENOSPC|disk is full",
     Problem("disk", "The disk is (almost) full",
             "Delete old folders in output/ or other large files; production resumes on its own.", False)),
    (r"No script backend|claude\.exe|Claude Code (?:was )?not found|setup-token",
     Problem("script_backend", "The script writer (Claude) is not available",
             "Open the Claude desktop app once so it finishes updating, or check the Claude token in Settings.", False)),
    (r"free local writer: no draft passed",
     Problem("local_writer", "Claude was unavailable and the free local writer's drafts failed the quality checks",
             "Check why Claude is unavailable (desktop app open and signed in, usage limit). Nothing was published.",
             True)),
    (r"repeated subjects|repeats a video already made",
     Problem("repeats", "Every draft repeated a subject already made",
             "Nothing to do: the next slot picks another blueprint. If this keeps happening, refresh the trends.", True)),
    (r"NoAudioReceived|edge.?tts|No audio was received",
     Problem("voice", "The voice service returned no audio", "Nothing to do unless it keeps happening.", True)),
    (r"ffmpeg failed|ffmpeg.*exit|CalledProcessError.*ffmpeg|Could not open encoder|Error submitting",
     Problem("render", "The video render failed", "Nothing to do unless it keeps happening.", True)),
    (r"Can't reach the API|ServerNotFound|Unable to find the server|getaddrinfo|Name or service not known|"
     r"timed out|TimeoutError|WinError 100(?:60|54|53)|ConnectionError|Connection reset|Connection aborted|"
     r"RemoteDisconnected|SSLError|EOF occurred|Max retries exceeded|BrokenPipe|503|502|500 Internal|backendError",
     Problem("network", "Network problem", "Nothing to do unless it keeps happening; then check the internet "
             "connection.", True)),
    (r"rate.?limit|429|Too Many Requests|Sign in to confirm",
     Problem("rate_limit", "YouTube is rate limiting trend discovery", "Nothing to do: the next refresh tries again.",
             True)),
]


def classify(error: str | None) -> Problem:
    text = error or ""
    for pattern, problem in _RULES:
        if re.search(pattern, text, re.I):
            return problem
    first = text.strip().splitlines()[0][:160] if text.strip() else "unknown error"
    return Problem("other:" + re.sub(r"\W+", "_", first[:40]).lower(), "Job failed", first, False)


# ---------------------------------------------------------------------------------------------------- sending

def _enabled() -> bool:
    from .notify import telegram_configured
    tcfg = (load_config().get("notifications") or {}).get("telegram") or {}
    return telegram_configured() and tcfg.get("alerts", True)


def send(key: str, text: str, *, repeat_hours: float = ALERT_REPEAT_HOURS) -> bool:
    """Send `text` unless the same `key` was sent within `repeat_hours`. Never raises."""
    if not _enabled():
        return False
    with _LOCK:
        state = load_json(STATE_PATH) or {}
        sent = state.setdefault("sent", {})
        if time.time() - float(sent.get(key, 0)) < repeat_hours * 3600:
            return False
        sent[key] = time.time()
        save_json(STATE_PATH, state)
    try:
        from .notify import send_message
        send_message(text)
        return True
    except Exception as exc:  # noqa: BLE001 - an alert must never break the job that failed
        log.warning("Telegram alert failed: %s", exc)
        return False


_KIND_NAMES = {"produce": "Production", "upload": "Upload", "run": "Trend refresh", "publish": "Publish",
               "channel": "Channel sync", "thumbnail": "Thumbnail", "profile": "Channel analysis"}


def job_failed(kind: str, params: dict[str, Any], error: str | None, *, retry_note: str = "") -> None:
    problem = classify(error)
    what = _KIND_NAMES.get(kind, kind)
    origin = "scheduled " if params.get("scheduled") else ("automatic " if params.get("auto") else "")
    target = params.get("path") or params.get("blueprint_key") or ""
    lines = [f"⚠️ {origin}{what} failed: {problem.title}",
             f"Fix: {problem.fix}"]
    if retry_note:
        lines.append(retry_note)
    if target:
        lines.append(f"({Path(str(target)).name})")
    if problem.key.startswith("other:"):
        lines.append(f"Error: {(error or '')[:300]}")
    send(f"job:{problem.key}", "\n".join(lines))


# ---------------------------------------------------------------------------------------------------- daily report

def _today_jobs(day: datetime) -> dict[str, dict[str, int]]:
    stamp = day.strftime("%Y-%m-%d")
    counts: dict[str, dict[str, int]] = {}
    if not LOG_DIR.exists():
        return counts
    for p in LOG_DIR.glob(f"{stamp}T*.log"):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                head = f.readline()
        except OSError:
            continue
        m = re.search(r"kind=(\w+) status=(\w+)", head)
        if m:
            counts.setdefault(m.group(1), {}).setdefault(m.group(2), 0)
            counts[m.group(1)][m.group(2)] += 1
    return counts


def _top_recent_videos(n: int = 3) -> list[dict[str, Any]]:
    data = load_json(DATA_DIR / "channel_stats.json") or {}
    recent = [v for v in data.get("videos", []) if float(v.get("age_hours") or 1e9) <= 7 * 24]
    recent.sort(key=lambda v: -float(v.get("views_per_hour") or 0))
    return recent[:n]


def daily_report(now: datetime | None = None) -> str:
    from . import housekeeping, upload_queue

    now = now or datetime.now()
    jobs = _today_jobs(now)
    prod, up = jobs.get("produce", {}), jobs.get("upload", {})
    q = upload_queue.summary()
    lines = [f"📊 Daily report, {now.strftime('%a %d %b')}",
             f"Produced: {prod.get('done', 0)} · failed: {prod.get('error', 0)}",
             f"Uploaded in the last 24 h: {q['uploaded_24h']} of {q['limit']} allowed"
             + (f" · upload errors today: {up.get('error', 0)}" if up.get("error") else ""),
             f"Waiting in the upload queue: {q['queued']}" + (f" · not uploaded (too old): {q['expired_today']}" if q["expired_today"] else "")]
    if q.get("paused_until"):
        lines.append(f"Uploads paused until {q['paused_until']} ({q.get('paused_reason', '')})")
    failures = [k for k, v in jobs.items() if v.get("error") and k not in ("produce", "upload")]
    if failures:
        lines.append("Other failures: " + ", ".join(f"{_KIND_NAMES.get(k, k)} x{jobs[k]['error']}" for k in failures))
    top = _top_recent_videos()
    if top:
        lines.append("Best videos this week (views per hour):")
        lines += [f"  {float(v.get('views_per_hour') or 0):.1f}/h · {int(v.get('views') or 0):,} views · {v['title'][:60]}"
                  for v in top]
    lines.append(f"Disk: {housekeeping.free_gb():.1f} GB free · footage cache {housekeeping.cache_size_gb():.1f} GB")
    return "\n".join(lines)


def maybe_send_daily_report(now: datetime | None = None) -> bool:
    """Send the report once a day at notifications.telegram.daily_report_hour (default 22)."""
    now = now or datetime.now()
    tcfg = (load_config().get("notifications") or {}).get("telegram") or {}
    hour = tcfg.get("daily_report_hour", 22)
    if hour is None or hour is False or now.hour < int(hour) or not _enabled():
        return False
    state = load_json(STATE_PATH) or {}
    today = now.strftime("%Y-%m-%d")
    if state.get("report_day") == today:
        return False
    state["report_day"] = today
    save_json(STATE_PATH, state)
    try:
        from .notify import send_message
        send_message(daily_report(now))
        log.info("daily report sent to Telegram")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("daily report failed: %s", exc)
        return False


def next_report_time(now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    hour = ((load_config().get("notifications") or {}).get("telegram") or {}).get("daily_report_hour", 22)
    if hour is None or hour is False:
        return None
    t = now.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)
