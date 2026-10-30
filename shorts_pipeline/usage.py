"""Cost and usage: what each day and each job spent, in the console and the daily report.

TypeSafe tokens (every system_one call is counted by wrapping the SDK once), Claude Code calls and their reported
cost, AI images and the pollen they cost, YouTube API quota units (search 100, upload 1600, update 50, others 1),
all per day and per job in data/usage.json. Alerts when the YouTube quota passes 80% or a configured daily budget.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date
from typing import Any

from .config import DATA_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

PATH = DATA_DIR / "usage.json"
_LOCK = threading.Lock()
CURRENT_JOB: str | None = None            # set by the console while a job runs (jobs run one at a time)
KEYS = ("typesafe_calls", "typesafe_in", "typesafe_out", "claude_calls", "claude_in", "claude_out", "claude_cost_usd",
        "images", "pollen", "youtube_units")
POLLEN_PRICES = {"black-forest-labs/flux.2-klein-4b": 0.005, "tongyi-mai/z-image-turbo": 0.004,
                 "black-forest-labs/flux.1-schnell": 0.002}
YOUTUBE_QUOTA = 10_000
_installed = False


def record(**amounts: float) -> None:
    """Add amounts (any of KEYS) to today's totals and the running job's."""
    with _LOCK:
        data = load_json(PATH) or {}
        day = data.setdefault(date.today().isoformat(), {"jobs": {}})
        for k, v in amounts.items():
            if k in KEYS and v:
                day[k] = round(float(day.get(k, 0)) + float(v), 6)
                if CURRENT_JOB:
                    j = day["jobs"].setdefault(CURRENT_JOB, {})
                    j[k] = round(float(j.get(k, 0)) + float(v), 6)
        save_json(PATH, data)
    _maybe_alert(day)


def today() -> dict[str, Any]:
    data = load_json(PATH) or {}
    day = dict(data.get(date.today().isoformat()) or {})
    day.pop("jobs", None)
    for k in KEYS:
        day.setdefault(k, 0)
    day["youtube_quota"] = YOUTUBE_QUOTA
    return day


def job(job_id: str) -> dict[str, Any]:
    data = load_json(PATH) or {}
    for day in data.values():
        if job_id in (day.get("jobs") or {}):
            return day["jobs"][job_id]
    return {}


def summary_text(day: dict[str, Any] | None = None) -> str:
    d = day or today()
    parts = [f"TypeSafe {int(d['typesafe_calls'])} calls / {int(d['typesafe_in'] + d['typesafe_out']) / 1000:.0f}k tokens"]
    if d["claude_calls"]:
        from .config import env
        on_api = bool(env("ANTHROPIC_API_KEY"))
        cost = f"${d['claude_cost_usd']:.2f}"
        parts.append(f"Claude {int(d['claude_calls'])} calls" + (f" ({cost} API)" if on_api else
                                                                f" (subscription; {cost} at API prices)" if d["claude_cost_usd"] else " (subscription)"))
    if d["images"]:
        parts.append(f"{int(d['images'])} AI images" + (f" ({d['pollen']:.3f} pollen)" if d["pollen"] else ""))
    parts.append(f"YouTube API {int(d['youtube_units']):,}/{YOUTUBE_QUOTA:,} units")
    return " · ".join(parts)


def _maybe_alert(day: dict[str, Any]) -> None:
    try:
        from .alerts import send
        if float(day.get("youtube_units", 0)) >= 0.8 * YOUTUBE_QUOTA:
            send("usage:youtube", f"⚠️ YouTube API quota at {int(day['youtube_units']):,} of {YOUTUBE_QUOTA:,} units today; "
                                  "trend refreshes and uploads may fail until midnight Pacific time.", repeat_hours=12)
        budgets = ((load_config().get("notifications") or {}).get("budgets")) or {}
        tokens = float(day.get("typesafe_in", 0)) + float(day.get("typesafe_out", 0))
        if budgets.get("typesafe_tokens_per_day") and tokens >= float(budgets["typesafe_tokens_per_day"]):
            send("usage:typesafe", f"⚠️ TypeSafe used {tokens / 1e6:.1f}M tokens today, over your budget of "
                                   f"{float(budgets['typesafe_tokens_per_day']) / 1e6:.1f}M.", repeat_hours=12)
        if budgets.get("pollen_per_day") and float(day.get("pollen", 0)) >= float(budgets["pollen_per_day"]):
            send("usage:pollen", f"⚠️ AI images used {day['pollen']:.2f} pollen today, over your budget of {budgets['pollen_per_day']}.",
                 repeat_hours=12)
    except Exception:  # noqa: BLE001 - never let accounting break a job
        pass


def install() -> None:
    """Count every TypeSafe call made anywhere in the engine (wraps the SDK's system_one once)."""
    global _installed
    if _installed:
        return
    try:
        import typesafe_sdk
        original = typesafe_sdk.TypeSafeClient.system_one

        def counted(self, *args, **kwargs):
            resp = original(self, *args, **kwargs)
            try:
                u = getattr(resp, "usage", None)
                record(typesafe_calls=1, typesafe_in=getattr(u, "input_tokens", 0) or 0, typesafe_out=getattr(u, "output_tokens", 0) or 0)
            except Exception:  # noqa: BLE001
                pass
            return resp
        typesafe_sdk.TypeSafeClient.system_one = counted
        _installed = True
    except Exception as exc:  # noqa: BLE001
        log.debug("usage counting not installed: %s", exc)


def note_claude(result: dict[str, Any]) -> None:
    """Record one Claude Code call from its result object."""
    usage = result.get("modelUsage") or {}
    tin = tout = 0
    for m in usage.values() if isinstance(usage, dict) else []:
        if isinstance(m, dict):
            tin += int(m.get("inputTokens") or 0) + int(m.get("cacheReadInputTokens") or 0)
            tout += int(m.get("outputTokens") or 0)
    record(claude_calls=1, claude_in=tin, claude_out=tout, claude_cost_usd=float(result.get("total_cost_usd") or 0))


_ = time
