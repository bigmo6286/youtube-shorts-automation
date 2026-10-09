"""What the channel's own numbers say about how much to post and how long Shorts should be.

Both are learned on every channel sync from public uploads at least 48 hours old (views per hour over the first two
weeks, and YouTube Analytics retention when connected), and saved to data/tuning.json. Samples are small and the
channel changes over time, so both move gradually and stay inside safe bounds.

Daily volume: days are grouped by how many Shorts went public (1-5, 6-10, 11-15, 16-25, 26+). For each group the
expected views of a day are its typical upload count times the median views per hour of one of its uploads; the best
group's typical count becomes the recommended daily upload limit, moved at most halfway from the previous value.

Length: uploads are grouped by duration; each group is scored like the channel factor (median views per hour against
the channel median)^0.6 x (median % watched against the channel median)^0.4; the best group with enough videos sets the
target script length, per format when that format has enough data of its own, else for the channel.
"""
from __future__ import annotations

import logging
import statistics as st
import time
from datetime import datetime
from typing import Any

from .config import DATA_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

TUNING_PATH = DATA_DIR / "tuning.json"
MIN_AGE_HOURS = 48
VOLUME_BUCKETS = [(1, 5), (6, 10), (11, 15), (16, 25), (26, 1000)]
LENGTH_BINS = [(0, 25), (26, 35), (36, 45), (46, 60), (61, 180)]
MIN_DAYS_PER_BUCKET = 2
MIN_VIDEOS_PER_BIN = 5
DEFAULTS = {"learn_volume": True, "learn_length": True, "min_daily": 3, "min_seconds": 18, "max_seconds": 55}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("schedule") or {}).get("tuning")) or {})
    return cfg


def _public_mature() -> list[dict[str, Any]]:
    data = load_json(DATA_DIR / "channel_stats.json") or {}
    return [v for v in data.get("videos", []) if v.get("privacy", "public") == "public"
            and float(v.get("age_hours") or 0) >= MIN_AGE_HOURS]


# ---------------------------------------------------------------------------------------------------- volume
def learn_volume(videos: list[dict[str, Any]], previous: int | None, ceiling: int, floor: int) -> dict[str, Any]:
    by_day: dict[str, list[float]] = {}
    for v in videos:
        day = datetime.fromisoformat(v["published"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(float(v.get("views_per_hour") or 0))
    buckets = []
    for lo, hi in VOLUME_BUCKETS:
        days = {d: xs for d, xs in by_day.items() if lo <= len(xs) <= hi}
        if len(days) < MIN_DAYS_PER_BUCKET:
            continue
        per_upload = st.median([x for xs in days.values() for x in xs])
        typical = int(round(st.median(len(xs) for xs in days.values())))
        buckets.append({"uploads": f"{lo}-{hi if hi < 1000 else '+'}", "days": len(days), "typical": typical,
                        "median_vph_per_upload": round(per_upload, 3), "expected_day_vph": round(typical * per_upload, 2)})
    if not buckets:
        return {"recommended": previous, "buckets": [], "reason": "not enough days with data yet"}
    best = max(buckets, key=lambda b: b["expected_day_vph"])
    target = max(floor, min(ceiling, best["typical"]))
    start = previous if previous is not None else ceiling          # first run: start from the configured limit
    recommended = int(round(start + (target - start) * 0.5))
    recommended = max(floor, min(ceiling, recommended))
    return {"recommended": recommended, "target": target, "best_bucket": best["uploads"], "buckets": buckets,
            "reason": f"days with {best['uploads']} uploads earned the most views per day"}


# ---------------------------------------------------------------------------------------------------- length
def _bin_scores(videos: list[dict[str, Any]], channel_vph: float, channel_pct: float | None) -> list[dict[str, Any]]:
    out = []
    for lo, hi in LENGTH_BINS:
        b = [v for v in videos if lo <= int(v.get("duration") or 0) <= hi]
        if len(b) < MIN_VIDEOS_PER_BIN:
            continue
        vph = st.median(float(v.get("views_per_hour") or 0) for v in b)
        pcts = [float(v["avg_view_pct"]) for v in b if v.get("avg_view_pct") is not None]
        vf = max(0.2, min(5.0, vph / channel_vph)) if channel_vph > 0 else 1.0
        rf = max(0.5, min(2.0, st.median(pcts) / channel_pct)) if pcts and channel_pct else 1.0
        out.append({"seconds": f"{lo}-{hi}", "videos": len(b), "median_vph": round(vph, 3),
                    "median_pct": round(st.median(pcts), 1) if pcts else None,
                    "score": round(vf ** 0.6 * rf ** 0.4, 3), "typical": int(st.median(int(v["duration"]) for v in b))})
    return out


def learn_lengths(videos: list[dict[str, Any]], default: int, lo: int, hi: int) -> dict[str, Any]:
    from .analytics import retention_for
    for v in videos:
        r = retention_for(v["id"])
        if r:
            v["avg_view_pct"] = r["avg_view_pct"]
    if not videos:
        return {"channel": default, "formats": {}, "bins": []}
    channel_vph = st.median(float(v.get("views_per_hour") or 0) for v in videos) or 0.01
    pcts = [v["avg_view_pct"] for v in videos if v.get("avg_view_pct") is not None]
    channel_pct = st.median(pcts) if pcts else None

    def pick(vs: list[dict[str, Any]]) -> tuple[int | None, list[dict[str, Any]]]:
        bins = _bin_scores(vs, channel_vph, channel_pct)
        if len(bins) < 2:
            return None, bins
        best = max(bins, key=lambda b: b["score"])
        return max(lo, min(hi, best["typical"])), bins

    overall, bins = pick(videos)
    result = {"channel": overall or default, "bins": bins, "formats": {}}
    from .channel import labelled_uploads
    labels = {u["id"]: u.get("format") for u in labelled_uploads()}
    by_format: dict[str, list[dict[str, Any]]] = {}
    for v in videos:
        if labels.get(v["id"]):
            by_format.setdefault(labels[v["id"]], []).append(v)
    for fmt, vs in by_format.items():
        if len(vs) >= 2 * MIN_VIDEOS_PER_BIN:
            secs, _ = pick(vs)
            if secs:
                result["formats"][fmt] = secs
    return result


# ---------------------------------------------------------------------------------------------------- entry points
def update() -> dict[str, Any]:
    """Recompute and save (called on every channel sync)."""
    cfg = config()
    prev = load_json(TUNING_PATH) or {}
    videos = _public_mature()
    prod = load_config().get("production") or {}
    ceiling = int((load_config().get("upload") or {}).get("daily_limit", 20))
    out = {"updated": time.time(), "videos": len(videos)}
    out["volume"] = learn_volume(videos, (prev.get("volume") or {}).get("recommended"), ceiling, int(cfg["min_daily"]))
    default = int(prod.get("target_seconds", 40))
    learned = learn_lengths(videos, default, int(cfg["min_seconds"]), int(cfg["max_seconds"]))
    before = prev.get("length") or {}
    step = lambda old, new: int(round(old + (new - old) * 0.5))  # noqa: E731 - move halfway per sync
    learned["target_channel"] = learned["channel"]
    learned["channel"] = step(int(before.get("channel") or default), learned["channel"])
    learned["formats"] = {f: step(int((before.get("formats") or {}).get(f) or learned["channel"]), secs)
                          for f, secs in learned["formats"].items()}
    out["length"] = learned
    save_json(TUNING_PATH, out)
    log.info("tuning: %s uploads/day recommended (%s); length %ss for the channel%s",
             out["volume"].get("recommended"), out["volume"].get("reason", ""), out["length"]["channel"],
             (", per format " + ", ".join(f"{k} {v}s" for k, v in out["length"]["formats"].items())) if out["length"]["formats"] else "")
    return out


def daily_upload_limit() -> int | None:
    if not config()["learn_volume"]:
        return None
    return ((load_json(TUNING_PATH) or {}).get("volume") or {}).get("recommended")


def target_seconds(fmt: str | None, default: int) -> int:
    if not config()["learn_length"]:
        return default
    length = (load_json(TUNING_PATH) or {}).get("length") or {}
    return int((length.get("formats") or {}).get(fmt or "") or length.get("channel") or default)
