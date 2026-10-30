"""Calendar awareness: what is coming up, so a Short about it lands a few days early.

Fixed and movable holidays (US and Nigeria by default), plus Wikipedia's free "On this day" feed for anniversaries.
The writer's prompt gets the next few days' events and is told to use one only when it fits the blueprint and topic;
the daily report lists them too.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

import requests

from .config import load_config
from .storage import JsonCache

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "lead_days": 6, "on_this_day": True, "regions": ["US", "NG"]}
_OTD = JsonCache("on_this_day")
FIXED = {
    "all": {(1, 1): "New Year's Day", (2, 14): "Valentine's Day", (3, 8): "International Women's Day", (3, 14): "Pi Day",
            (3, 17): "St Patrick's Day", (4, 1): "April Fools' Day", (4, 22): "Earth Day", (10, 31): "Halloween",
            (12, 24): "Christmas Eve", (12, 25): "Christmas Day", (12, 31): "New Year's Eve"},
    "US": {(7, 4): "US Independence Day", (11, 11): "Veterans Day", (6, 19): "Juneteenth"},
    "NG": {(10, 1): "Nigeria Independence Day", (6, 12): "Nigeria Democracy Day", (5, 1): "Workers' Day"},
}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("schedule") or {}).get("seasonal")) or {})
    return cfg


def easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = ((h + m - 7 * n + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def events_for_year(year: int, regions: list[str]) -> dict[date, str]:
    out: dict[date, str] = {}
    for region in ["all", *regions]:
        for (m, d), name in FIXED.get(region, {}).items():
            out[date(year, m, d)] = name
    e = easter(year)
    out[e] = "Easter Sunday"
    out[e - timedelta(days=2)] = "Good Friday"
    out[_nth_weekday(year, 5, 6, 2)] = "Mother's Day (US)"
    out[_nth_weekday(year, 6, 6, 3)] = "Father's Day (US)"
    if "US" in regions:
        tg = _nth_weekday(year, 11, 3, 4)
        out[tg] = "Thanksgiving (US)"
        out[tg + timedelta(days=1)] = "Black Friday"
    return out


def upcoming(today: date | None = None, lead_days: int | None = None) -> list[dict[str, Any]]:
    cfg = config()
    today = today or date.today()
    lead = int(lead_days if lead_days is not None else cfg["lead_days"])
    events = events_for_year(today.year, cfg["regions"]) | events_for_year(today.year + 1, cfg["regions"])
    rows = []
    for d, name in sorted(events.items()):
        days = (d - today).days
        if 0 <= days <= lead:
            rows.append({"date": d.isoformat(), "name": name, "days": days})
    return rows


TRAGEDY = re.compile(r"(kill|killed|killing|dead|death|deaths|dies|died|massacre|crash|crashes|collapse|collapses|attack|"
                     r"bomb|bombing|shoot|shooting|murder|terror|earthquake|tsunami|genocide|executed|assassinat)", re.I)
MIN_AGE_YEARS = 20            # anniversaries, not last year's news


def _pick_anniversaries(rows: list[dict[str, Any]], year: int, limit: int) -> list[str]:
    """Older, round-number anniversaries first (100th, 50th, 25th, 10th...), no tragedies."""
    def roundness(age: int) -> int:
        return 3 if age % 100 == 0 else 2 if age % 50 == 0 else 1 if age % 25 == 0 or age % 10 == 0 else 0
    good = []
    for r in rows:
        try:
            age = year - int(r["year"])
        except (TypeError, ValueError):
            continue
        if age < MIN_AGE_YEARS or TRAGEDY.search(r["text"]):
            continue
        good.append((roundness(age), age, r))
    good.sort(key=lambda x: (-x[0], -x[1]))
    return [f"{r['year']} ({year - int(r['year'])} years ago): {r['text']}" for _, _, r in good[:limit]]


def on_this_day(day: date, limit: int = 4) -> list[str]:
    """Notable anniversaries on this calendar day (Wikipedia's free feed), cached: older and round-number ones first,
    nothing tragic (a facts channel does not need death-count bait)."""
    key = f"{day.month:02d}-{day.day:02d}"
    hit = _OTD.get(key)
    if isinstance(hit, list) and hit and isinstance(hit[0], dict):
        return _pick_anniversaries(hit, day.year, limit)
    try:
        from .commons import USER_AGENT
        r = requests.get(f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{day.month:02d}/{day.day:02d}",
                         headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        events = r.json().get("events") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("on this day failed: %s", exc)
        return []
    rows = [{"year": e.get("year"), "text": e.get("text", "").strip()[:160]} for e in events if e.get("text")]
    _OTD.set(key, rows[:60])
    return _pick_anniversaries(rows, day.year, limit)


def prompt_note(today: date | None = None) -> str:
    cfg = config()
    if not cfg["enabled"]:
        return ""
    today = today or date.today()
    lines = []
    for ev in upcoming(today):
        when = "today" if ev["days"] == 0 else "tomorrow" if ev["days"] == 1 else f"in {ev['days']} days ({ev['date']})"
        lines.append(f"- {ev['name']} {when}")
    if cfg.get("on_this_day"):
        for offset in range(0, 3):
            d = today + timedelta(days=offset)
            for text in on_this_day(d, limit=2):
                lines.append(f"- On this day {d.strftime('%b %d')}, {text[:140]}")
    if not lines:
        return ""
    return ("\nCalendar (use ONE of these only if it genuinely fits this format and topic; otherwise ignore them):\n"
            + "\n".join(lines[:8]) + "\n")


def report_lines(today: date | None = None) -> list[str]:
    ups = upcoming(today)
    return [f"Coming up: {', '.join(f'{e['name']} ({e['days']} d)' for e in ups)}"] if ups else []
