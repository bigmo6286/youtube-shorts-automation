"""Voice casting: the narrator and the pace are chosen per Short, and each voice's retention is learned.

Instead of rotating narrators by title hash (originality.py), TypeSafe hears the script's mood and subject and picks
among the configured voices (each described in `production.voice_casting.voices`) and a pace (slow for solemn or
emotional subjects, brisk for fast facts). Code multiplies the choice probabilities by what the channel has learned:
a voice's median percentage viewed (YouTube Analytics) against the best voice's, once it has `min_videos` uploads,
so a narrator viewers leave sooner is cast less. Without TypeSafe the rotation stays as it was.
"""
from __future__ import annotations

import logging
import statistics as st
from typing import Any

from .config import OUTPUT_DIR, has_typesafe, load_config
from .storage import load_json

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "learn": True, "min_videos": 5,
            "voices": {"en-US-AndrewNeural": "warm, calm, friendly: stories, nature, history told gently",
                       "en-US-BrianNeural": "bright, energetic, youthful: fast facts, surprising science, lists",
                       "en-US-ChristopherNeural": "deep, steady, authoritative: mysteries, serious history, big numbers"},
            "rates": {"slow": "+2%", "normal": "+8%", "brisk": "+14%"}}
PACES = {"slow": "solemn, emotional, sacred or heavy subjects, told with weight (a bible story, a tragedy, a quiet wonder)",
         "normal": "most explainers: a clear story with a payoff",
         "brisk": "quick facts, lists, surprising science or a punchy comparison that wants energy"}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("voice_casting")) or {})
    if not isinstance(cfg.get("voices"), dict) or not cfg["voices"]:
        cfg["voices"] = dict(DEFAULTS["voices"])
    cfg["rates"] = {**DEFAULTS["rates"], **(cfg.get("rates") or {})}
    return cfg


def voice_stats(output_dir=None) -> dict[str, dict[str, Any]]:
    """Per voice: uploads with retention data and their median percentage viewed."""
    from .analytics import retention_for
    output_dir = output_dir or OUTPUT_DIR
    rows: dict[str, list[float]] = {}
    if not output_dir.exists():
        return {}
    for d in output_dir.iterdir():
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if not meta or not meta.get("youtube_id") or not meta.get("voice"):
            continue
        ret = retention_for(meta["youtube_id"])
        if ret and ret.get("avg_view_pct"):
            rows.setdefault(meta["voice"], []).append(float(ret["avg_view_pct"]))
    return {v: {"videos": len(x), "median_view_pct": round(st.median(x), 1)} for v, x in rows.items()}


def factors(cfg: dict[str, Any] | None = None) -> dict[str, float]:
    """Multiplier per configured voice from its retention against the best voice (1.0 until learned)."""
    cfg = cfg or config()
    out = {v: 1.0 for v in cfg["voices"]}
    if not cfg.get("learn", True):
        return out
    try:
        stats = voice_stats()
    except Exception:  # noqa: BLE001
        return out
    known = {v: s["median_view_pct"] for v, s in stats.items() if v in out and s["videos"] >= int(cfg["min_videos"])}
    if len(known) >= 2:
        best = max(known.values())
        for v, pct in known.items():
            out[v] = round(max(0.3, pct / best), 3) if best else 1.0
    return out


def choose(script: dict[str, Any], blueprint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """{voice, rate, pace, probabilities, factors} for this script, or None (keep the rotation). Never raises."""
    cfg = config()
    if not cfg["enabled"] or not has_typesafe():
        return None
    voices = list(cfg["voices"].items())
    try:
        from typesafe_sdk import Choice, TypeSafeClient
        state = {"title": script.get("title"), "hook": script.get("hook"), "script": (script.get("full_text") or "")[:1500],
                 "topic": (blueprint or {}).get("topic"), "format": (blueprint or {}).get("format")}
        questions = {
            "voice": Choice(instructions="Which narrator suits this script's subject and mood best for a faceless YouTube Short?",
                            criteria={name: desc for name, desc in voices}),
            "pace": Choice(instructions="At what pace should it be read?", criteria=PACES),
        }
        with TypeSafeClient(timeout=60.0) as client:
            resp = client.system_one(state=state, questions=questions)
        probs = {k: float(v) for k, v in resp.answers["voice"].probabilities.items()}
        pace = str(resp.answers["pace"].choice)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice casting skipped (%s)", str(exc)[:140])
        return None
    fac = factors(cfg)
    scored = {v: probs.get(v, 0.0) * fac.get(v, 1.0) for v, _ in voices}
    voice = max(scored, key=scored.get)
    rate = cfg["rates"].get(pace) or cfg["rates"]["normal"]
    log.info("voice casting: %s (%.0f%% fit x%.2f learned), %s pace (%s)", voice, probs.get(voice, 0) * 100, fac.get(voice, 1.0), pace, rate)
    return {"voice": voice, "rate": rate, "pace": pace, "probabilities": {k: round(v, 3) for k, v in probs.items()}, "factors": fac}
