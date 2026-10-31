"""Subject screening: choose what the Short is about before writing it.

Claude proposes several candidate subjects inside the blueprint or topic (each a one-line idea with its surprise),
given everything the channel has already made and the questions people are asking. TypeSafe scores each one:
curiosity pull, how visual it is (footage or images can show it), whether it answers a demand question, and
whether it repeats a subject already made. Code weighs those and hands the winner to the writer as the angle, so the
full script is only written for the best idea.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .config import has_typesafe, load_config

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "candidates": 6}
WEIGHTS = {"curiosity": 0.5, "visual": 0.2, "demand": 0.2, "fresh": 0.1}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("subject_screening")) or {})
    return cfg


class _Subject(BaseModel):
    subject: str = Field(description="The specific story, fact, person, event or thing the Short would be about, in one line")
    surprise: str = Field(description="The one surprising or emotional thing in it that makes people stop, in one sentence")


class _Subjects(BaseModel):
    items: list[_Subject]


PROPOSE_SYSTEM = ("You find subjects for faceless narrated YouTube Shorts. Each subject must be specific (one story, one fact, one "
                  "person or thing), true and checkable, filmable with stock footage or a generated image, and genuinely surprising "
                  "to a general viewer. Never reuse a subject from the taken list, even reworded.")


def propose(blueprint: dict[str, Any], avoid: list[str], demand: list[str], backend: str = "auto") -> list[dict[str, str]]:
    from . import script_gen
    n = int(config()["candidates"])
    series = blueprint.get("series")
    topic = series or f"{blueprint.get('topic', '')} ({blueprint.get('format', '')} format)"
    prompt = (f"Propose {n} different subjects for a Short in the format '{blueprint.get('format')}' about: {topic}.\n"
              + (f"Series: every Short is one story or fact inside '{series}'.\n" if series else "")
              + ("Questions people are asking about it (a subject answering one is good):\n" + "\n".join(f"- {q}" for q in demand[:8]) + "\n" if demand else "")
              + ("Taken subjects (never these, even reworded):\n" + "\n".join(f"- {t}" for t in avoid[:40]) + "\n" if avoid else "")
              + "Spread the subjects: different eras, places or angles, not six variations of one idea.")
    chosen = script_gen.pick_backend(backend)
    if chosen == "ollama":
        return []
    import anthropic
    client = anthropic.Anthropic() if chosen == "api" else None
    out = script_gen._draft(client, PROPOSE_SYSTEM, prompt, chosen, model_cls=_Subjects)
    return [{"subject": s.subject.strip(), "surprise": s.surprise.strip()} for s in out.items if s.subject.strip()]


def screen(candidates: list[dict[str, str]], blueprint: dict[str, Any], avoid: list[str],
           demand: list[str]) -> list[dict[str, Any]]:
    """Each candidate with its scores and total, best first."""
    if not candidates:
        return []
    if not has_typesafe():
        return [{**c, "total": 0.5} for c in candidates]
    from typesafe_sdk import Noul, Score, TypeSafeClient
    state = {"topic": blueprint.get("series") or blueprint.get("topic"), "format": blueprint.get("format"),
             "candidates": [{"id": i, **c} for i, c in enumerate(candidates)],
             "taken_titles": avoid[:40], "asked_questions": demand[:10]}
    questions: dict[str, Any] = {}
    for i, _ in enumerate(candidates):
        questions[f"cur_{i}"] = Score(instructions=f"How strong is the curiosity pull of `candidates[{i}]` for a general viewer "
                                                   "scrolling Shorts: would they need to know how it ends?",
                                      criteria=["none: ordinary or well known", "mild: somewhat interesting",
                                                "strong: a real 'wait, what?' moment", "irresistible: surprising and emotional"])
        questions[f"vis_{i}"] = Score(instructions=f"How well can `candidates[{i}]` be shown on screen with stock footage, real photos "
                                                   "or generated images?",
                                      criteria=["abstract: nothing to show", "partly: generic footage only", "well: concrete scenes exist"])
        questions[f"dem_{i}"] = Noul(instructions=f"Does `candidates[{i}]` answer or closely match one of `asked_questions`?")
        questions[f"rep_{i}"] = Noul(instructions=f"Is `candidates[{i}]` the same story, fact, person or thing as any title in "
                                                  "`taken_titles` (not merely the same broad topic)?")
    with TypeSafeClient(timeout=90.0) as client:
        resp = client.system_one(state=state, questions=questions)
    rows = []
    for i, c in enumerate(candidates):
        cur = float(resp.answers[f"cur_{i}"].score) / 3
        vis = float(resp.answers[f"vis_{i}"].score) / 2
        dem = float(resp.answers[f"dem_{i}"].noul)
        rep = float(resp.answers[f"rep_{i}"].noul)
        total = WEIGHTS["curiosity"] * cur + WEIGHTS["visual"] * vis + WEIGHTS["demand"] * dem + WEIGHTS["fresh"] * (1 - rep)
        if rep >= 0.6:
            total = -1.0                                   # a repeat never wins, however strong
        rows.append({**c, "curiosity": round(cur * 3, 2), "visual": round(vis * 2, 2), "demand": round(dem, 2),
                     "repeat": round(rep, 2), "total": round(total, 3)})
    rows.sort(key=lambda r: -r["total"])
    return rows


def choose(blueprint: dict[str, Any], avoid: list[str], backend: str = "auto") -> dict[str, Any] | None:
    """The best subject for this production, or None (the writer then picks its own as before)."""
    if not config()["enabled"]:
        return None
    try:
        from .demand import questions
        demand = [q["text"] for q in questions(blueprint.get("series") or str(blueprint.get("topic") or "").replace("_", " "))]
    except Exception:  # noqa: BLE001
        demand = []
    try:
        cands = propose(blueprint, avoid, demand, backend)
        ranked = screen(cands, blueprint, avoid, demand)
    except Exception as exc:  # noqa: BLE001
        log.warning("subject screening skipped (%s)", str(exc)[:140])
        return None
    if not ranked:
        return None
    best = ranked[0]
    log.info("subjects: %d proposed; best %r (curiosity %.1f/3, visual %.1f/2, demand %.0f%%, repeat %.0f%%)%s",
             len(ranked), best["subject"][:70], best["curiosity"], best["visual"], best["demand"] * 100, best["repeat"] * 100,
             "; runners-up: " + "; ".join(r["subject"][:40] for r in ranked[1:3]) if len(ranked) > 1 else "")
    if best["repeat"] >= 0.6:
        return None
    return {**best, "demand_questions": demand[:8], "all": ranked}
