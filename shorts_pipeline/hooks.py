"""Hook tournament: several opening sentences compete, the best one is voiced.

The hook decides most of a Short's result (the channel's post-mortems and retention curves both say so). After a
script passes QA, Claude writes alternative first sentences for the same script; TypeSafe scores every candidate
(and the original) for scroll-stopping power and specificity, and, for a looping Short, whether the closing bridge
still leads into it. Code applies the channel's retention lesson on hook length and keeps the original unless a
challenger is clearly better.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from .config import has_typesafe, load_config

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "candidates": 4, "min_gain": 0.05}
LOOP_MIN = 0.4
LONG_PENALTY = 0.15


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("hook_tournament")) or {})
    return cfg


class _Hooks(BaseModel):
    items: list[str] = Field(description="Alternative first sentences, each complete on its own")


def _max_words() -> int | None:
    """The hook length the channel's retention curves favour, if learned ('Hooks of 13 words or fewer...')."""
    try:
        from .retention import guidance
        for line in guidance():
            m = re.search(r"Hooks of (\d+) words or fewer", line)
            if m:
                return int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return None


def alternatives(script: dict[str, Any], blueprint: dict[str, Any], backend: str = "auto") -> list[str]:
    from . import script_gen
    n = int(config()["candidates"])
    max_words = _max_words()
    body = " ".join(ln["text"] for ln in script.get("lines") or [])
    prompt = (f"Write {n} alternative FIRST sentences (hooks) for this Short. Each must be true to the script, specific and "
              "surprising, stop a scroller within two seconds, and promise exactly what the script delivers. Vary the technique: "
              "a bold claim, a question, a scene, a number. "
              + (f"At most {max_words} words each (this channel's viewers leave during longer hooks). " if max_words else "Keep them short. ")
              + (f"The script loops: its last line is {script.get('cta')!r} and must still lead naturally into each hook.\n" if script.get("cta") else "\n")
              + f"Current hook: {script.get('hook')!r}\nTitle: {script.get('title')!r}\nRest of the script: {body}")
    chosen = script_gen.pick_backend(backend)
    if chosen == "ollama":
        return []
    import anthropic
    client = anthropic.Anthropic() if chosen == "api" else None
    out = script_gen._draft(client, "You write opening lines for faceless narrated YouTube Shorts.", prompt, chosen, model_cls=_Hooks)
    seen = {(script.get("hook") or "").strip().lower()}
    alts = []
    for h in out.items:
        h = h.strip()
        if h and h.lower() not in seen and 3 <= len(h.split()) <= 30:
            seen.add(h.lower())
            alts.append(h)
    return alts[:n]


def judge(script: dict[str, Any], candidates: list[str], loop: bool) -> list[dict[str, Any]]:
    """Every candidate (the original first) with its scores and total."""
    if not has_typesafe():
        return []
    from typesafe_sdk import Noul, Score, TypeSafeClient
    body = " ".join(ln["text"] for ln in script.get("lines") or [])
    state = {"title": script.get("title"), "body": body, "closing_line": script.get("cta"),
             "hooks": [{"id": i, "text": h} for i, h in enumerate(candidates)]}
    questions: dict[str, Any] = {}
    for i, _ in enumerate(candidates):
        questions[f"stop_{i}"] = Score(instructions=f"How strong is `hooks[{i}].text` as the first sentence of this Short at stopping "
                                                    "a viewer from scrolling, given `body` is what follows?",
                                       criteria=["No hook: generic intro or slow setup", "Weak: states the subject, no reason to stay",
                                                 "Good: creates a specific question or promise", "Strong: surprising, specific and impossible to leave unanswered"])
        questions[f"spec_{i}"] = Noul(instructions=f"Is `hooks[{i}].text` concrete and specific (a particular thing, number, person or "
                                                   "scene) rather than vague or clickbait that `body` does not deliver?")
        if loop:
            questions[f"loop_{i}"] = Noul(instructions=f"When the Short replays, does `closing_line` lead naturally into `hooks[{i}].text` "
                                                       "so the two read as one continuous thought?")
    with TypeSafeClient(timeout=60.0) as client:
        resp = client.system_one(state=state, questions=questions)
    max_words = _max_words()
    rows = []
    for i, h in enumerate(candidates):
        stop = float(resp.answers[f"stop_{i}"].score) / 3
        spec = float(resp.answers[f"spec_{i}"].noul)
        lp = float(resp.answers[f"loop_{i}"].noul) if loop else 1.0
        total = 0.6 * stop + 0.4 * spec
        if max_words and len(h.split()) > max_words:
            total -= LONG_PENALTY
        rows.append({"hook": h, "stop": round(stop * 3, 2), "specific": round(spec, 2), "loops": round(lp, 2),
                     "total": round(total, 3), "original": i == 0})
    return rows


def run(script: dict[str, Any], blueprint: dict[str, Any], *, loop: bool, backend: str = "auto") -> dict[str, Any]:
    """Replace the script's hook with a clearly better alternative, if there is one. Returns the script."""
    cfg = config()
    if not cfg["enabled"] or not script.get("hook"):
        return script
    try:
        alts = alternatives(script, blueprint, backend)
        if not alts:
            return script
        rows = judge(script, [script["hook"], *alts], loop)
    except Exception as exc:  # noqa: BLE001
        log.warning("hook tournament skipped (%s)", str(exc)[:140])
        return script
    if not rows:
        return script
    original = rows[0]
    # The closing bridge was written for the original hook, so a challenger only has to loop nearly as well as it.
    loop_floor = min(LOOP_MIN, original["loops"] - 0.1)
    eligible = [r for r in rows[1:] if not loop or r["loops"] >= loop_floor]
    best = max(eligible, key=lambda r: r["total"]) if eligible else None
    script["hook_tournament"] = {"original": original, "candidates": rows[1:], "chosen": (best or original)["hook"]}
    if best and best["total"] >= original["total"] + float(cfg["min_gain"]):
        log.info("hook tournament: %r (%.2f) replaces %r (%.2f)", best["hook"][:70], best["total"], original["hook"][:70], original["total"])
        script["hook"] = best["hook"]
        script["full_text"] = " ".join([script["hook"], *[ln["text"] for ln in script.get("lines") or []], script.get("cta", "")]).strip()
        script["word_count"] = len(script["full_text"].split())
    else:
        log.info("hook tournament: the original hook stays (%.2f vs best challenger %.2f)", original["total"],
                 best["total"] if best else 0.0)
    return script
