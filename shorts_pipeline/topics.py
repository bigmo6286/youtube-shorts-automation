"""Topic series: "bible stories", "space facts", "Nigerian history" -> a stream of different Shorts inside that topic.

`produce --topic "bible stories"` (or the Studio form, or `schedule.source: topic` with `schedule.topics`) makes one
Short of the series. For each one the engine picks a format and a hook style: TypeSafe says once per topic which
narrated formats suit it (a Choice over the formats the engine can make without a person on camera) and which of
the engine's topic labels it belongs to (so the channel feedback and learned lengths apply); code then rotates
through the suited formats, weighted by how each performs on your channel, never the same format twice in a row.
The writer is told the series and every subject already made in it, so each Short is a fresh story or fact inside
the topic. Topics whose claims come from a text or tradition (scripture, mythology, fiction) are told as that
tradition tells them, with the source named, not as verified history.
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from .config import DATA_DIR, has_typesafe
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "topics.json"
# Formats a faceless, narrated Short can be: no person on camera, no gameplay, no product in hand.
NARRATED_FORMATS = ["storytime", "history_story", "single_fact", "listicle_facts", "explainer", "myth_busting",
                    "mystery_unsolved", "quiz_riddle", "comparison_versus", "then_vs_now", "quote_wisdom",
                    "motivational", "psychology_trick", "ranking_top_list"]
HOOKS = ["question", "bold_claim", "curiosity_gap", "story_open"]
MIN_FORMAT_SHARE = 0.08         # a format suits the topic when the Choice gives it at least this probability
ATTRIBUTE_AT = 0.6              # P(claims come from a text or tradition) from which the writer must name the source


def slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.strip().lower()).strip("_")[:40] or "topic"


def _state() -> dict[str, Any]:
    return load_json(STATE_PATH) or {"topics": {}}


def describe(topic: str) -> dict[str, Any]:
    """What this topic is, judged once by TypeSafe and remembered: suited formats with their shares, the engine's
    topic label, and whether its claims come from a text or tradition. Without TypeSafe: a sensible default."""
    st = _state()
    hit = st["topics"].get(slug(topic), {}).get("profile")
    if hit and hit.get("topic_text") == topic.strip():
        return hit
    profile = {"topic_text": topic.strip(), "formats": {"storytime": 0.4, "single_fact": 0.3, "explainer": 0.3},
               "label": "other", "attribute_source": False, "judged": False}
    if has_typesafe():
        try:
            profile.update(_judge_topic(topic))
            profile["judged"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("topic judgement failed (%s); using the default formats", str(exc)[:120])
    st["topics"].setdefault(slug(topic), {})["profile"] = profile
    save_json(STATE_PATH, st)
    return profile


def _judge_topic(topic: str) -> dict[str, Any]:
    from typesafe_sdk import Choice, Noul, TypeSafeClient
    from .judge import FORMATS, TOPICS
    state = {"topic": topic, "note": "a series of faceless narrated YouTube Shorts (AI voice over footage) about this topic"}
    questions = {
        "format": Choice(instructions={"question": "Which Short format fits videos in this series best? The probability "
                                                   "spread across options says how much of the series each format should be.",
                                       "note": "Several formats can suit one topic; spread the probability over all that do."},
                         criteria={f: FORMATS[f] for f in NARRATED_FORMATS}),
        "label": Choice(instructions="Which topic label describes this series?", criteria=dict(TOPICS)),
        "attribute": Noul(instructions=("Do the claims in videos about `topic` come mainly from a text or tradition "
                                        "(scripture, mythology, folklore, fiction, a religion's teaching) rather than from "
                                        "verifiable history or science, so the videos should say which text or tradition "
                                        "they are retelling?")),
    }
    with TypeSafeClient(timeout=60.0) as client:
        resp = client.system_one(state=state, questions=questions)
    probs = {k: float(v) for k, v in resp.answers["format"].probabilities.items()}
    suited = {k: round(v, 3) for k, v in probs.items() if v >= MIN_FORMAT_SHARE}
    if not suited:
        suited = {max(probs, key=probs.get): 1.0}
    return {"formats": suited, "label": resp.answers["label"].choice,
            "attribute_source": float(resp.answers["attribute"].noul) >= ATTRIBUTE_AT}


def plan(topic: str) -> dict[str, Any]:
    """The blueprint for the next Short of the series: a suited format (weighted by its share and by how the format
    performs on your channel, not the one used last time) and a rotating hook style."""
    profile = describe(topic)
    st = _state()
    mine = st["topics"].setdefault(slug(topic), {})
    weights = dict(profile["formats"])
    try:
        from .channel import blueprint_performance
        perf = (blueprint_performance().get("formats") or {})
        for f in weights:
            weights[f] *= float((perf.get(f) or {}).get("factor") or 1.0)
    except Exception:  # noqa: BLE001 - no channel data yet
        pass
    last = mine.get("last_format")
    if len(weights) > 1 and last in weights:
        weights = {f: w for f, w in weights.items() if f != last}
    fmt = random.choices(list(weights), weights=list(weights.values()))[0]
    hook = HOOKS[int(mine.get("count", 0)) % len(HOOKS)]
    return {"format": fmt, "topic": profile["label"], "hook_style": hook, "source": "topic",
            "series": topic.strip(), "key": f"series:{slug(topic)}", "attribute_source": profile["attribute_source"],
            "why_it_works": f"one of a series on {topic.strip()}: a fresh subject inside the topic every time"}


def record(topic: str, fmt: str, out_dir: str) -> None:
    st = _state()
    mine = st["topics"].setdefault(slug(topic), {})
    mine.update(last_format=fmt, count=int(mine.get("count", 0)) + 1, last_at=time.time(), last_dir=out_dir)
    save_json(STATE_PATH, st)


def next_topic(topics: list[str]) -> str | None:
    """Round robin over the configured topics: the one produced longest ago."""
    topics = [t.strip() for t in topics if t and t.strip()]
    if not topics:
        return None
    st = _state()
    return min(topics, key=lambda t: float(st["topics"].get(slug(t), {}).get("last_at") or 0))
