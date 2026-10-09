"""Special productions that use what the channel already knows: sequels of winners and viewers' requests.

Sequels: a public upload whose views per hour beat the channel median by `sequel_factor` (and with at least
`sequel_min_views` views, 48 h+ old, not below the channel's median retention when Analytics is connected) gets one
"Part 2": same subject and format, new facts or a new chapter of the story, with part 1 linked in the description.
When part 2 is public, a comment under part 1 points to it.

Viewer requests: comments that ask for a topic (TypeSafe `idea` judgment during comment triage) become a Short in the
format and topic of the video they were left on, opening from the viewer's question. When it is public, the engine
replies to the viewer with the link.

At most `sequels_per_day` / `ideas_per_day` of these replace a normal scheduled production; state in data/specials.json.
"""
from __future__ import annotations

import logging
import re
import statistics as st
import time
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "specials.json"
DEFAULTS = {"sequels": True, "sequels_per_day": 1, "sequel_factor": 3.0, "sequel_min_views": 300,
            "ideas": True, "ideas_per_day": 1, "min_idea": 0.6, "explore_share": 0.2}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((load_config().get("schedule") or {}).get("specials") or {})
    return cfg


def _state() -> dict[str, Any]:
    return load_json(STATE_PATH) or {"sequels": {}, "ideas": {}, "made": []}


def _save(state: dict[str, Any]) -> None:
    save_json(STATE_PATH, state)


def _made_today(state: dict[str, Any], kind: str) -> int:
    day = time.strftime("%Y-%m-%d")
    return sum(1 for m in state.get("made", []) if m.get("kind") == kind and m.get("day") == day)


# ---------------------------------------------------------------------------------------------------- sequels
def sequel_candidates() -> list[dict[str, Any]]:
    from .channel import labelled_uploads
    cfg = config()
    state = _state()
    ups = [u for u in labelled_uploads() if u.get("privacy", "public") == "public" and float(u.get("age_hours") or 0) >= 48]
    if len(ups) < 10:
        return []
    med_vph = st.median(float(u.get("views_per_hour") or 0) for u in ups) or 0.01
    pcts = [u["avg_view_pct"] for u in ups if u.get("avg_view_pct") is not None]
    med_pct = st.median(pcts) if pcts else None
    out = []
    for u in ups:
        if u["id"] in state["sequels"] or re.search(r"\bpart\s*\d+\b", u["title"].lower()):    # never a sequel of a sequel
            continue
        if int(u.get("views") or 0) < int(cfg["sequel_min_views"]):
            continue
        if float(u.get("views_per_hour") or 0) < float(cfg["sequel_factor"]) * med_vph:
            continue
        if med_pct is not None and u.get("avg_view_pct") is not None and u["avg_view_pct"] < med_pct:
            continue
        out.append({**u, "lift": round(float(u["views_per_hour"]) / med_vph, 1)})
    return sorted(out, key=lambda u: -u["lift"])


def sequel_blueprint(video_id: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Blueprint, angle and the part-1 record for a sequel of `video_id`."""
    from .channel import labelled_uploads
    part1 = next((u for u in labelled_uploads() if u["id"] == video_id), None)
    if not part1:
        raise RuntimeError(f"video {video_id} is not among the channel's labelled uploads; run a channel sync")
    bp = {"format": part1.get("format") or "storytime", "topic": part1.get("topic") or "other",
          "hook_style": part1.get("hook_style") or "curiosity_gap", "source": "sequel",
          "why_it_works": f"part 1, {part1['title']!r}, earned {int(part1.get('views') or 0):,} views, far above this channel's usual",
          "exemplars": [{"title": part1["title"], "transcript": (part1.get("transcript") or "")[:500]}]}
    angle = (f"This is PART 2 of the channel's Short {part1['title']!r}, which viewers loved. Same subject, new material: "
             "more facts or the next chapter of the story that part 1 did not cover; never repeat part 1's facts. The title "
             "must end with '(Part 2)'. Part 1's opening was: " + repr((part1.get("transcript") or "")[:300]))
    return bp, angle, part1


# ---------------------------------------------------------------------------------------------------- viewer ideas
def idea_candidates() -> list[dict[str, Any]]:
    cfg = config()
    from .comments import _state as comment_state
    threads = comment_state().get("threads", {})
    used = _state()["ideas"]
    out = [dict(t, id=k) for k, t in threads.items()
           if float(t.get("idea") or 0) >= float(cfg["min_idea"]) and k not in used and t.get("status") != "spam"]
    return sorted(out, key=lambda t: (-float(t.get("idea") or 0), -(t.get("likes") or 0)))


def idea_blueprint(thread_id: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    from .channel import labelled_uploads
    from .comments import _state as comment_state
    thread = comment_state()["threads"].get(thread_id)
    if not thread:
        raise RuntimeError(f"comment {thread_id} is not known; run a comment check")
    video = next((u for u in labelled_uploads() if u["id"] == thread.get("video_id")), {}) or {}
    bp = {"format": video.get("format") or "explainer", "topic": video.get("topic") or "other",
          "hook_style": "question", "source": "viewer_idea",
          "why_it_works": f"a viewer asked for it under {video.get('title', 'one of the channel videos')!r}"}
    angle = (f"A viewer asked under the channel's video {video.get('title', '')!r}: {thread['text'][:300]!r}. Make this Short "
             "answer or cover exactly what they asked, accurately; open with their question in your own words.")
    return bp, angle, thread


# ---------------------------------------------------------------------------------------------------- scheduling
def next_special() -> dict[str, Any] | None:
    """Production parameters for a special that is due now, or None."""
    cfg = config()
    state = _state()
    if cfg["sequels"] and _made_today(state, "sequel") < int(cfg["sequels_per_day"]):
        cands = sequel_candidates()
        if cands:
            c = cands[0]
            log.info("special: a sequel of %r (x%.1f the channel's typical views per hour)", c["title"][:60], c["lift"])
            return {"sequel_of": c["id"]}
    if cfg["ideas"] and _made_today(state, "idea") < int(cfg["ideas_per_day"]):
        ideas = idea_candidates()
        if ideas:
            log.info("special: a viewer's request: %r", ideas[0]["text"][:80])
            return {"idea": ideas[0]["id"]}
    return None


def record(kind: str, key: str, out_dir: str) -> None:
    state = _state()
    bucket = "sequels" if kind == "sequel" else "ideas"
    state[bucket][key] = {"out_dir": out_dir, "at": time.time()}
    state.setdefault("made", []).append({"kind": kind, "key": key, "day": time.strftime("%Y-%m-%d"), "out_dir": out_dir})
    state["made"] = state["made"][-200:]
    _save(state)


# ---------------------------------------------------------------------------------------------------- follow-ups
def post_followups(yt) -> int:
    """Once a sequel or a viewer's Short is public: link part 2 under part 1, and reply to the viewer."""
    from .upload import with_retries
    state = _state()
    done = 0
    for bucket in ("sequels", "ideas"):
        for key, rec in state[bucket].items():
            if rec.get("followed_up"):
                continue
            meta = load_json(OUTPUT_DIR / rec["out_dir"] / "meta.json") if rec.get("out_dir") else None
            if not meta or meta.get("privacy") != "public" or not meta.get("youtube_id"):
                continue
            link = f"https://youtube.com/shorts/{meta['youtube_id']}"
            try:
                if bucket == "sequels":
                    body = {"snippet": {"videoId": key, "topLevelComment": {"snippet": {"textOriginal": f"Part 2 is out: {link}"}}}}
                    with_retries(lambda: yt.commentThreads().insert(part="snippet", body=body).execute(), "link part 2")
                else:
                    body = {"snippet": {"parentId": key, "textOriginal": f"You asked, so we made it: {link}"}}
                    with_retries(lambda: yt.comments().insert(part="snippet", body=body).execute(), "reply to the viewer")
            except Exception as exc:  # noqa: BLE001
                log.warning("follow-up comment failed: %s", str(exc)[:160])
                continue
            rec["followed_up"] = time.time()
            done += 1
    if done:
        _save(state)
    return done
