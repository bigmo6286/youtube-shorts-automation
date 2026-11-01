"""Per-video post-mortems: why a Short did much better or worse than the channel's usual, and what to learn.

Two days after a Short goes public (enough views to mean something), its views per hour are compared with the
channel's median. For clear winners and clear losers, TypeSafe is given the script, the numbers and the retention
holds and asked for the main reason among a fixed set of causes (hook, subject, footage match, length, title,
timing, format fatigue, or plain distribution noise). Code keeps the verdicts, counts the causes over the recent
reviews, and turns them into one or two lessons for the writer's prompt and the daily report.
"""
from __future__ import annotations

import logging
import statistics as st
import time
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR, has_typesafe
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "postmortems.json"
MIN_AGE_HOURS = 48
MIN_VIEWS = 50
PER_RUN = 6
WINNER, LOSER = 1.5, 0.6                 # factor against the channel median views/hour
CAUSES = {
    "hook": "the first sentence: too slow, vague or generic (loser) / unusually sharp and surprising (winner)",
    "subject": "the subject itself: too familiar or niche (loser) / unusually curious, fresh or emotional (winner)",
    "footage": "what was on screen: footage or images that did not match or looked generic (loser) / striking visuals (winner)",
    "length": "the length: too long for its idea, or too rushed (loser) / just right (winner)",
    "title": "the title: unclear or misleading (loser) / a strong search or curiosity title (winner)",
    "timing": "when it went public: a dead hour or a crowded day (loser) / a good slot (winner)",
    "format_fatigue": "the channel had done this format x topic many times recently, so regulars skipped it",
    "noise": "nothing in the video explains it: the Shorts feed did not pick it up (see traffic), distribution luck, a feed "
             "test, or too few views to tell",
}


def _state() -> dict[str, Any]:
    return load_json(STATE_PATH) or {"reviews": {}}


def _outputs() -> list[tuple[Any, dict[str, Any], dict[str, Any]]]:
    out = []
    if not OUTPUT_DIR.exists():
        return out
    for d in OUTPUT_DIR.iterdir():
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if meta and meta.get("youtube_id") and meta.get("channel_stats"):
            out.append((d, meta, load_json(d / "script.json") or {}))
    return out


def candidates() -> list[dict[str, Any]]:
    """Public Shorts at least MIN_AGE_HOURS old with enough views and no review yet, with their factor."""
    reviewed = _state()["reviews"]
    outs = _outputs()
    rates = [float(m["channel_stats"].get("views_per_hour") or 0) for _, m, _ in outs if float(m["channel_stats"].get("views_per_hour") or 0) > 0]
    if len(rates) < 5:
        return []
    median = st.median(rates)
    rows = []
    for d, meta, script in outs:
        cs = meta["channel_stats"]
        vid = meta["youtube_id"]
        if vid in reviewed or float(cs.get("age_hours") or 0) < MIN_AGE_HOURS or int(cs.get("views") or 0) < MIN_VIEWS:
            continue
        factor = float(cs.get("views_per_hour") or 0) / median if median else 1.0
        rows.append({"dir": d, "meta": meta, "script": script, "factor": round(factor, 2), "median": round(median, 2)})
    rows.sort(key=lambda r: abs(r["factor"] - 1), reverse=True)      # the clearest cases first
    return rows


def _traffic(video_id: str) -> Any:
    """Feed / search / suggested shares of this Short against the channel's usual, when known."""
    try:
        from .traffic import split_for
        s = split_for(video_id)
    except Exception:  # noqa: BLE001
        s = None
    if not s:
        return "not measured"
    return {"views_counted": s["views"], "from_shorts_feed": f"{s['feed']:.0%} (channel usual {s.get('channel_feed', 0):.0%})",
            "from_search": f"{s['search']:.0%} (channel usual {s.get('channel_search', 0):.0%})",
            "from_suggested": f"{s['suggested']:.0%} (channel usual {s.get('channel_suggested', 0):.0%})"}


def review(row: dict[str, Any]) -> dict[str, Any]:
    """One Short's verdict: outcome, main cause (TypeSafe) and the numbers it was judged on."""
    meta, script, factor = row["meta"], row["script"], row["factor"]
    outcome = "winner" if factor >= WINNER else "loser" if factor <= LOSER else "average"
    verdict = {"title": meta.get("title", ""), "factor": factor, "outcome": outcome, "at": time.time(),
               "format": (meta.get("blueprint") or {}).get("format"), "topic": (meta.get("blueprint") or {}).get("topic"),
               "views": int(meta["channel_stats"].get("views") or 0)}
    if outcome == "average" or not has_typesafe():
        verdict["cause"] = None
        return verdict
    holds = {}
    try:
        from .retention import CURVES_PATH, measure
        curve = ((load_json(CURVES_PATH) or {}).get(meta["youtube_id"]) or {}).get("curve")
        if curve:
            m = measure(curve, meta, script)
            if m:
                holds = {k: m[k] for k in ("hook_hold", "end_hold", "worst_section", "start")}
    except Exception:  # noqa: BLE001
        pass
    from typesafe_sdk import Choice, Noul, TypeSafeClient
    state = {"outcome": f"{outcome}: {factor:.1f}x the channel's median views per hour",
             "title": meta.get("title"), "hook": script.get("hook"), "script": script.get("full_text"),
             "format": verdict["format"], "topic": verdict["topic"], "hook_style": (meta.get("blueprint") or {}).get("hook_style"),
             "duration_seconds": meta.get("duration"), "published": meta["channel_stats"].get("published"),
             "retention": holds or "not measured", "footage": [s.get("path", "").split("\\")[-1] for s in (meta.get("segments") or [])][:8],
             "traffic": _traffic(meta["youtube_id"])}
    questions = {
        "cause": Choice(instructions={"question": f"What is the main reason this Short was a {outcome} on this channel?",
                                      "note": "Judge from the script, title, numbers and retention; when nothing in the video "
                                              "explains the result, choose noise."}, criteria=CAUSES),
        "repeatable": Noul(instructions=("Is the reason something the writer can act on in the next scripts (a lesson), "
                                         "rather than a one-off circumstance?")),
    }
    try:
        with TypeSafeClient(timeout=60.0) as client:
            resp = client.system_one(state=state, questions=questions)
        verdict["cause"] = resp.answers["cause"].choice
        verdict["cause_confidence"] = round(float(resp.answers["cause"].confidence or 0), 2)
        verdict["repeatable"] = round(float(resp.answers["repeatable"].noul), 2)
        verdict["holds"] = holds
    except Exception as exc:  # noqa: BLE001
        log.warning("post-mortem for %s skipped: %s", meta.get("title", "")[:50], str(exc)[:120])
        verdict["cause"] = None
    return verdict


def run(max_new: int = PER_RUN) -> int:
    st_ = _state()
    n = 0
    for row in candidates()[:max_new]:
        v = review(row)
        st_["reviews"][row["meta"]["youtube_id"]] = v
        n += 1
        if v.get("cause"):
            log.info("post-mortem: %s (%.1fx) %r -> %s", v["outcome"], v["factor"], v["title"][:50], v["cause"])
    if n:
        save_json(STATE_PATH, st_)
    return n


def lessons(recent: int = 12) -> list[str]:
    """What the recent reviews agree on, as sentences for the writer and the report."""
    reviews = sorted(_state()["reviews"].values(), key=lambda v: -float(v.get("at") or 0))[:recent]
    out = []
    for outcome, verb in (("loser", "underperformed"), ("winner", "won")):
        group = [v for v in reviews if v.get("outcome") == outcome and v.get("cause") and v["cause"] != "noise"
                 and float(v.get("repeatable") or 0) >= 0.5]
        if len(group) < 3:
            continue
        counts: dict[str, int] = {}
        for v in group:
            counts[v["cause"]] = counts.get(v["cause"], 0) + 1
        cause, n = max(counts.items(), key=lambda kv: kv[1])
        if n < 2:
            continue
        advice = {
            ("loser", "hook"): "open with the single most surprising fact, in one short sentence",
            ("loser", "subject"): "choose subjects with a stronger surprise or emotional pull; skip the familiar ones",
            ("loser", "footage"): "name the exact scene in each visual_keyword so the footage matches the line",
            ("loser", "length"): "cut to the idea: fewer lines, each one doing work",
            ("loser", "title"): "make the title state the concrete surprise, not a vague tease",
            ("loser", "timing"): "nothing for the writer; the publish times will adjust",
            ("loser", "format_fatigue"): "vary the format and topic more between consecutive Shorts",
            ("winner", "hook"): "keep opening with one sharp, specific, surprising sentence",
            ("winner", "subject"): "keep picking subjects with a built-in 'wait, what?' moment",
            ("winner", "footage"): "keep the visuals literal and striking",
            ("winner", "length"): "keep this length and pace",
            ("winner", "title"): "keep titles that state the concrete surprise",
        }.get((outcome, cause), "")
        out.append(f"{n} of the last {len(group)} Shorts that {verb} did so because of the {cause.replace('_', ' ')}"
                   + (f": {advice}." if advice else "."))
    return out


def summary() -> dict[str, Any]:
    reviews = list(_state()["reviews"].values())
    return {"reviewed": len(reviews), "winners": sum(v.get("outcome") == "winner" for v in reviews),
            "losers": sum(v.get("outcome") == "loser" for v in reviews), "lessons": lessons()}
