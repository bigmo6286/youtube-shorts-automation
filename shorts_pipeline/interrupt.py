"""Pattern interrupt at the drop-off point: a re-hook where this channel's viewers leave.

The retention curves (retention.py) say at what fraction of a Short the steepest loss happens on this channel (about
a third of viewers reach the ending; most leave early in the body). This module puts something there on purpose:
  - writing: the script prompt names the spot, and after QA the line that sits on it is judged by TypeSafe for how
    strongly it reopens curiosity (a new question, twist or promise). A weak line is rewritten by Claude with the
    same facts; the rewrite is judged too and only replaces the original when it is clearly stronger and adds no claim;
  - picture and sound: at that line's first word the render gets a quick punch-in, a big on-screen callout with the
    key number or name of the moment (chosen by Claude, checked by TypeSafe against the script) and a low hit.
Without curves yet, the spot defaults to `default_at` of the video. Long videos and user-written scripts are left alone.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .config import has_typesafe, load_config

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "rewrite": True, "card": True, "punch": True, "hit": True, "default_at": 0.42,
            "min_rehook": 0.67, "card_seconds": 1.3}
PUNCH_AMOUNT = 0.10         # the punch-in: 10% larger at the cut, settling over PUNCH_SECONDS
PUNCH_SECONDS = 0.35
MIN_LINES = 3               # a body shorter than this has no middle to interrupt


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("interrupt")) or {})
    return cfg


def drop_at() -> tuple[float, str]:
    """(fraction of the video where viewers leave fastest, 'learned' | 'default')."""
    cfg = config()
    try:
        from .retention import drop_point
        learned = drop_point()
    except Exception:  # noqa: BLE001
        learned = None
    if learned:
        return max(0.15, min(0.8, float(learned))), "learned"
    return float(cfg["default_at"]), "default"


def _counts(script: dict[str, Any]) -> list[int]:
    return [len((script.get("hook") or "").split()), *[len(ln["text"].split()) for ln in script.get("lines") or []],
            len((script.get("cta") or "").split())]


def line_index(script: dict[str, Any], frac: float) -> int | None:
    """The body line (index into script['lines']) that starts just before `frac` of the spoken words; never the first or
    the last body line."""
    lines = script.get("lines") or []
    if len(lines) < MIN_LINES:
        return None
    counts = _counts(script)
    total = sum(counts)
    if not total:
        return None
    target = frac * total
    start, idx = counts[0], 0
    for i, ln in enumerate(lines):
        if start > target:
            break
        idx = i
        start += counts[i + 1]
    return max(1, min(len(lines) - 2, idx))


def prompt_note(target_seconds: int) -> str:
    """One paragraph for the script prompt: where the re-hook belongs."""
    if not config()["enabled"]:
        return ""
    frac, basis = drop_at()
    words = int(target_seconds * 2.6)
    where = (f"YOUR viewers leave fastest about {frac:.0%} of the way into a Short (from the channel's retention curves)"
             if basis == "learned" else f"viewers of Shorts like this leave fastest about {frac:.0%} of the way in")
    return (f"\nRE-HOOK: {where}, around spoken word {int(frac * words)} of {words}. The line at that point must reopen "
            "curiosity: a new question, a twist, or a promise about what the next lines reveal (the kind of line that makes "
            "leaving feel like missing the best part), never a transition or a summary.\n")


class _Rehook(BaseModel):
    rehook_line: str = Field(description=(
        "A rewrite of the MARKED line that reopens curiosity right there: a new question, a twist, or a promise about what "
        "the following lines reveal. Keep its facts; add no new fact, number or claim; at most 18 words; natural for "
        "text-to-speech; it must still flow from the line before and into the line after."))
    visual_keyword: str = Field(description="Stock-footage search term for the rewritten line: a literal, filmable scene in 2-5 plain words")
    card_text: str = Field(description=(
        "The key number, name or phrase of this moment for big on-screen text: 1-5 words taken from the marked line or the "
        "lines right after it (e.g. '1 BILLION TONS', '52 HERTZ', 'NOBODY SURVIVED'), never an exaggeration or an invention."))


def _ask_claude(script: dict[str, Any], idx: int, backend: str) -> _Rehook | None:
    from . import script_gen
    chosen = script_gen.pick_backend(backend)
    if chosen == "ollama":
        return None
    import anthropic
    client = anthropic.Anthropic() if chosen == "api" else None
    lines = script["lines"]
    shown = [f"HOOK: {script.get('hook')}"]
    for i, ln in enumerate(lines):
        mark = "  <-- MARKED LINE, rewrite this one" if i == idx else ""
        shown.append(f"LINE {i + 1}: {ln['text']}{mark}")
    shown.append(f"CLOSING: {script.get('cta')}")
    prompt = ("This Short loses viewers at the marked line. Rewrite only that line so it reopens curiosity there, and give the "
              "big on-screen callout for that moment.\n\n" + "\n".join(shown))
    return script_gen._draft(client, "You improve scripts for faceless narrated YouTube Shorts (an AI voice over stock footage).",
                             prompt, chosen, model_cls=_Rehook, max_tokens=600)


def _judge(script: dict[str, Any], idx: int, rewrite: str | None, card: str | None) -> dict[str, float]:
    from typesafe_sdk import Noul, Score, TypeSafeClient
    lines = script["lines"]
    original = lines[idx]["text"]
    after = " ".join(ln["text"] for ln in lines[idx + 1:]) + " " + (script.get("cta") or "")
    before = (script.get("hook") or "") + " " + " ".join(ln["text"] for ln in lines[:idx])
    state = {"before": before.strip(), "original": original, "after": after.strip(), "rewrite": rewrite or "", "card_text": card or ""}
    crit = ["none: a plain statement or a transition", "weak: carries the story on without a new pull",
            "good: raises a clear new question or promise about what follows", "strong: a twist or promise that makes leaving now feel like missing the best part"]
    questions: dict[str, Any] = {
        "rehook_o": Score(instructions="How strongly does `original`, spoken at this point of the Short (after `before`, with `after` "
                                       "still to come), reopen the viewer's curiosity?", criteria=crit)}
    if rewrite:
        questions["rehook_r"] = Score(instructions="How strongly does `rewrite`, spoken in place of `original` (after `before`, with "
                                                   "`after` still to come), reopen the viewer's curiosity?", criteria=crit)
        questions["faithful"] = Noul(instructions="Does `rewrite` state only what `original`, `before` and `after` already say, "
                                                  "adding no new fact, number, name or claim?")
        questions["flows"] = Noul(instructions="Read `before`, then `rewrite`, then `after`: does it read as one continuous, "
                                               "natural narration?")
    if card:
        questions["card_ok"] = Noul(instructions="Is `card_text` a number, name or phrase that appears in `original`, `rewrite` or "
                                                 "`after` (or a faithful short form of one), not an exaggeration or an invention?")
    with TypeSafeClient(timeout=60.0) as client:
        resp = client.system_one(state=state, questions=questions)
    out = {"original": round(float(resp.answers["rehook_o"].score) / 3, 3)}
    if rewrite:
        out["rewrite"] = round(float(resp.answers["rehook_r"].score) / 3, 3)
        out["faithful"] = round(float(resp.answers["faithful"].noul), 3)
        out["flows"] = round(float(resp.answers["flows"].noul), 3)
    if card:
        out["card_ok"] = round(float(resp.answers["card_ok"].noul), 3)
    return out


def plan(script: dict[str, Any], backend: str = "auto") -> dict[str, Any]:
    """Decide the interrupt for a generated script: possibly rewrite the drop-point line, choose the callout. Mutates and
    returns the script; script['interrupt'] carries the decision. Never raises."""
    cfg = config()
    if not cfg["enabled"] or not script.get("lines"):
        return script
    frac, basis = drop_at()
    idx = line_index(script, frac)
    if idx is None:
        return script
    info: dict[str, Any] = {"line": idx, "at": round(frac, 3), "basis": basis, "original": script["lines"][idx]["text"],
                            "rewritten": False, "card_text": None, "scores": {}}
    if not has_typesafe():
        # no judge: the line and the callout are never changed on Claude's word alone; the punch-in and hit still land
        script["interrupt"] = info
        return script
    try:
        got = _ask_claude(script, idx, backend)
    except Exception as exc:  # noqa: BLE001
        log.warning("pattern interrupt: Claude step skipped (%s)", str(exc)[:140])
        got = None
    rewrite = (got.rehook_line.strip() if got and got.rehook_line else None)
    if rewrite and (rewrite.lower() == info["original"].lower() or not 3 <= len(rewrite.split()) <= 24):
        rewrite = None
    card = (got.card_text.strip() if got and got.card_text else None)
    if card and not 1 <= len(card.split()) <= 5:
        card = None
    try:
        scores = _judge(script, idx, rewrite if cfg["rewrite"] else None, card if cfg["card"] else None)
    except Exception as exc:  # noqa: BLE001
        log.warning("pattern interrupt: not judged (%s); the line stays", str(exc)[:140])
        scores = {}
    info["scores"] = scores
    strong = scores.get("original", 0.0) >= float(cfg["min_rehook"])
    if (cfg["rewrite"] and rewrite and not strong and scores.get("faithful", 0) >= 0.6 and scores.get("flows", 0) >= 0.5
            and scores.get("rewrite", 0) >= scores.get("original", 0) + 0.1):
        script["lines"][idx]["text"] = rewrite
        if got and got.visual_keyword.strip():
            script["lines"][idx]["visual_keyword"] = got.visual_keyword.strip()
        script["full_text"] = " ".join([script.get("hook") or "", *[ln["text"] for ln in script["lines"]], script.get("cta") or ""]).strip()
        script["word_count"] = len(script["full_text"].split())
        info["rewritten"] = True
        info["chosen"] = rewrite
    if cfg["card"] and card and scores.get("card_ok", 0) >= 0.6:
        info["card_text"] = card
    script["interrupt"] = info
    log.info("pattern interrupt at line %d (%s drop point %.0f%%): re-hook %s%s", idx + 1, basis, frac * 100,
             "rewritten" if info["rewritten"] else "kept", f", callout {info['card_text']!r}" if info["card_text"] else "")
    return script


def timing(script: dict[str, Any], words: list[dict[str, Any]]) -> float | None:
    """Body-local second where the interrupt line starts (its first word), from the TTS word timings."""
    info = script.get("interrupt") or {}
    lines = script.get("lines") or []
    if not info or not words or not lines or not config()["enabled"]:
        return None
    idx = int(info["line"])
    if not 0 <= idx < len(lines):
        return None
    counts = _counts(script)
    wi = sum(counts[: idx + 1])
    if wi >= len(words):
        return None
    return round(float(words[wi]["start"]), 3)
