"""When to make an uploaded Short public.

Automatic uploads go up private with a `publishAt` time, so YouTube publishes them by itself at an hour when the
channel's videos do best, after a review window in which the owner can cancel (Keep private) or publish at once.

Hour scores are learned from the channel: every public upload's publish hour (UTC) against its views per hour
(log scale, smoothed over neighbouring hours), shrunk toward a prior that favours US daytime and evening viewing
(most English Shorts audiences) so a handful of videos cannot dominate. As uploads accumulate, the channel's own
data takes over. Several Shorts can share a good hour, up to `max_per_hour`, at least `min_gap_minutes` apart.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR, load_config
from .storage import load_json

log = logging.getLogger(__name__)

DEFAULTS = {
    "schedule_publish": True,   # automatic uploads go public by themselves at a good hour (else they stay private)
    "review_hours": 2,          # earliest public time after the upload: your window to cancel or publish at once
    "max_per_hour": 2,          # at most this many Shorts go public in the same hour
    "publish_gap_minutes": 25,  # and at least this far apart
    "horizon_hours": 24,        # look at most this far ahead for a good hour (caps how long a Short waits)
}
PRIOR_WEIGHT = 8.0              # pseudo-videos per hour of prior belief
MIN_AGE_HOURS = 24              # a video needs a day of views before its publish hour says anything


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (load_config().get("upload") or {}).items() if k in DEFAULTS})
    return cfg


def _prior_by_utc_hour() -> list[float]:
    """Relative attractiveness of each UTC hour for an English-speaking (mostly US) audience: low overnight in
    the US, rising through the US afternoon, highest in the US evening. Values are multipliers around 1."""
    try:
        from zoneinfo import ZoneInfo
        ny_offset = datetime.now(ZoneInfo("America/New_York")).utcoffset().total_seconds() / 3600
    except Exception:  # noqa: BLE001
        ny_offset = -4.0
    curve = {0: .55, 1: .45, 2: .4, 3: .4, 4: .45, 5: .55, 6: .7, 7: .8, 8: .85, 9: .9, 10: .9, 11: .95,
             12: 1.05, 13: 1.05, 14: 1.0, 15: 1.05, 16: 1.1, 17: 1.15, 18: 1.2, 19: 1.25, 20: 1.25, 21: 1.2,
             22: 1.0, 23: .75}                                    # by New York local hour
    return [curve[int((h + ny_offset) % 24)] for h in range(24)]


def hour_scores() -> dict[str, Any]:
    """Score per UTC hour (higher is better), with how many videos support each hour."""
    data = load_json(DATA_DIR / "channel_stats.json") or {}
    vids = [v for v in data.get("videos", []) if v.get("privacy", "public") == "public"
            and float(v.get("age_hours") or 0) >= MIN_AGE_HOURS and v.get("published")]
    sums, counts = [0.0] * 24, [0.0] * 24
    for v in vids:
        h = datetime.fromisoformat(v["published"].replace("Z", "+00:00")).astimezone(timezone.utc).hour
        val = math.log1p(float(v.get("views_per_hour") or 0))
        for dh, w in ((-1, .25), (0, .5), (1, .25)):              # smooth over neighbouring hours
            sums[(h + dh) % 24] += w * val
            counts[(h + dh) % 24] += w
    overall = (sum(sums) / sum(counts)) if sum(counts) else 0.1
    prior = _prior_by_utc_hour()
    scores = []
    for h in range(24):
        prior_val = overall * prior[h]
        scores.append((sums[h] + PRIOR_WEIGHT * prior_val) / (counts[h] + PRIOR_WEIGHT))
    return {"scores": scores, "videos": [round(c * 2) / 2 for c in counts], "overall": overall, "n": len(vids)}


def _taken() -> list[datetime]:
    """Publish times already planned for Shorts that are not public yet."""
    out = []
    now = datetime.now(timezone.utc)
    if not OUTPUT_DIR.exists():
        return out
    for d in OUTPUT_DIR.iterdir():
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if meta and meta.get("publish_at") and meta.get("privacy") != "public":
            try:
                t = datetime.fromisoformat(meta["publish_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if t > now:
                out.append(t)
    return out


def choose(now: datetime | None = None) -> datetime:
    """The best publish time (UTC) after the review window, respecting the per-hour cap and spacing."""
    cfg = config()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    earliest = now + timedelta(hours=float(cfg["review_hours"]))
    scores = hour_scores()["scores"]
    top = max(scores) or 1.0
    taken = _taken()
    gap = timedelta(minutes=float(cfg["publish_gap_minutes"]))
    best: tuple[float, datetime] | None = None
    start = earliest.replace(minute=0, second=0, microsecond=0)
    for i in range(int(cfg["horizon_hours"]) + 2):
        hour_start = start + timedelta(hours=i)
        same_hour = [t for t in taken if hour_start <= t < hour_start + timedelta(hours=1)]
        if len(same_hour) >= int(cfg["max_per_hour"]):
            continue
        # first free minute in this hour that keeps the gap to every planned publish
        candidate = max(hour_start, earliest)
        for _ in range(12):
            clash = [t for t in taken if abs(t - candidate) < gap]
            if not clash:
                break
            candidate = max(clash) + gap
        if candidate >= hour_start + timedelta(hours=1):
            continue
        hours_ahead = (candidate - now).total_seconds() / 3600
        value = scores[candidate.hour] / top - 0.025 * hours_ahead   # a good hour today beats a slightly better one tomorrow
        if best is None or value > best[0]:
            best = (value, candidate)
    when = best[1] if best else earliest
    return when.replace(second=0, microsecond=0)


def rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def describe() -> dict[str, Any]:
    """For the console: best local hours and the next free publish time."""
    hs = hour_scores()
    offset = datetime.now().astimezone().utcoffset()
    local = [(int(((h * 3600 + offset.total_seconds()) // 3600) % 24), s) for h, s in enumerate(hs["scores"])]
    ranked = sorted(local, key=lambda x: -x[1])
    nxt = choose().astimezone()
    return {"best_local_hours": [h for h, _ in ranked[:6]], "videos": hs["n"], "next_publish": nxt.strftime("%a %H:%M"),
            "enabled": bool(config()["schedule_publish"])}
