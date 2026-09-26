"""Composite ranking. Raw judgments stay untouched; only the weights here decide the order."""
from __future__ import annotations

import math
from typing import Any

from .fetch import age_hours, probably_non_english
from .judge import normalized_score


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def score_shorts(shorts: list[dict[str, Any]], ranking_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach `signals`, `score` and `excluded` to each short; return sorted list (best first)."""
    w = dict(ranking_cfg.get("weights", {}))
    total_w = sum(w.values()) or 1.0
    w = {k: v / total_w for k, v in w.items()}

    velocities, engagements = [], []
    for s in shorts:
        hours = age_hours(s) or (24.0 * 365)
        views = float(s.get("view_count") or 0)
        s["_velocity"] = views / hours
        s["_engagement"] = (float(s.get("like_count") or 0) + 3.0 * float(s.get("comment_count") or 0)) / max(views, 1.0)
        velocities.append(math.log1p(s["_velocity"]))
        engagements.append(math.log1p(s["_engagement"] * 1000))
    v_norm, e_norm = _minmax(velocities), _minmax(engagements)

    for s, vn, en in zip(shorts, v_norm, e_norm):
        j = s.get("judgment")
        signals = {
            "velocity": vn,
            "engagement": en,
            "replicable": normalized_score(j, "replicable"),
            "hook": normalized_score(j, "hook_strength"),
            "evergreen": normalized_score(j, "evergreen"),
        }
        s["signals"] = signals
        s["views_per_hour"] = round(s.pop("_velocity"), 1)
        s["engagement_rate"] = round(s.pop("_engagement"), 5)
        s["score"] = round(sum(w.get(k, 0.0) * v for k, v in signals.items()), 4)

        reasons = []
        if probably_non_english(s):
            reasons.append("non-Latin script or non-English audio (code check)")
        if j:
            if j["english_ok"]["noul"] < ranking_cfg.get("min_english_noul", 0.6):
                reasons.append("not accessible to an English audience")
            if j["is_promo"]["noul"] > ranking_cfg.get("max_promo_noul", 0.6):
                reasons.append("promotional")
        s["excluded"] = reasons
    shorts.sort(key=lambda x: (bool(x["excluded"]), -x["score"]))
    return shorts
