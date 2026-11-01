"""Key-fact callouts: the payoff number, name or phrase of a line pops over the footage as big text for a second.

After a generated script passes QA, Claude names up to `max` body lines that carry a key number, name or striking
fact and the 1-5 words to show; TypeSafe checks each one is really in that line (or a faithful short form) and is the
line's point rather than filler. The producer draws them at the line's first word (captions.add_card_event), never
on the hook (it has its overlay) or on the pattern interrupt's line (it has its own callout), and never two within
`min_gap` seconds. Nothing is shown on Claude's word alone: without TypeSafe there are no callouts.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .config import has_typesafe, load_config

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "max": 3, "seconds": 1.2, "min_ok": 0.6, "min_gap": 5.0}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("callouts")) or {})
    return cfg


class _Callout(BaseModel):
    line: int = Field(description="The body line number as shown (1-based)")
    text: str = Field(description="1-5 words taken from that line: the key number, name or phrase (e.g. '1 BILLION TONS', '1347', 'MOUNT EREBUS')")


class _Callouts(BaseModel):
    items: list[_Callout] = Field(description="At most the number asked for; fewer when the script has fewer real payoffs")


def _ask_claude(script: dict[str, Any], skip: set[int], backend: str) -> list[dict[str, Any]]:
    from . import script_gen
    chosen = script_gen.pick_backend(backend)
    if chosen == "ollama":
        return []
    import anthropic
    client = anthropic.Anthropic() if chosen == "api" else None
    n = int(config()["max"])
    shown = [f"LINE {i + 1}: {ln['text']}" for i, ln in enumerate(script["lines"]) if i not in skip]
    prompt = (f"Pick at most {n} of these lines that carry the Short's key payoffs: a specific number, a name, a date or a "
              "striking fact a viewer should see written large. For each give the 1-5 words to show, copied from the line. "
              "Skip set-up lines and transitions.\n\n" + "\n".join(shown))
    out = script_gen._draft(client, "You design on-screen text for faceless narrated YouTube Shorts.", prompt, chosen,
                            model_cls=_Callouts, max_tokens=500)
    rows = []
    for it in out.items[:n]:
        idx = int(it.line) - 1
        text = " ".join(str(it.text).split())
        if 0 <= idx < len(script["lines"]) and idx not in skip and 1 <= len(text.split()) <= 5:
            rows.append({"line": idx, "text": text})
    return rows


def _judge(script: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from typesafe_sdk import Noul, TypeSafeClient
    state = {"items": [{"id": i, "line": script["lines"][r["line"]]["text"], "callout": r["text"]} for i, r in enumerate(rows)]}
    questions: dict[str, Any] = {}
    for i in range(len(rows)):
        questions[f"in_{i}"] = Noul(instructions=f"Is `items[{i}].callout` a number, name or phrase that appears in `items[{i}].line` "
                                                 "(or a faithful short form of one), not an exaggeration or an invention?")
        questions[f"key_{i}"] = Noul(instructions=f"Is `items[{i}].callout` the point of `items[{i}].line` (its key number, name or "
                                                  "surprising fact), rather than filler or a transition?")
    with TypeSafeClient(timeout=60.0) as client:
        resp = client.system_one(state=state, questions=questions)
    for i, r in enumerate(rows):
        r["in_line"] = round(float(resp.answers[f"in_{i}"].noul), 3)
        r["key"] = round(float(resp.answers[f"key_{i}"].noul), 3)
    return rows


def pick(script: dict[str, Any], backend: str = "auto") -> dict[str, Any]:
    """Choose the callouts for a generated script; script['callouts'] = [{line, text}]. Never raises."""
    cfg = config()
    if not cfg["enabled"] or not script.get("lines") or not has_typesafe():
        return script
    skip = set()
    if script.get("interrupt") and script["interrupt"].get("card_text"):
        skip.add(int(script["interrupt"]["line"]))
    try:
        rows = _ask_claude(script, skip, backend)
        if rows:
            rows = _judge(script, rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("callouts skipped (%s)", str(exc)[:140])
        return script
    keep = [r for r in rows if r.get("in_line", 0) >= float(cfg["min_ok"]) and r.get("key", 0) >= float(cfg["min_ok"])]
    keep.sort(key=lambda r: r["line"])
    script["callouts"] = [{"line": r["line"], "text": r["text"]} for r in keep]
    log.info("callouts: %s", ", ".join(f"line {r['line'] + 1} {r['text']!r}" for r in keep) or "none")
    return script


def times(script: dict[str, Any], words: list[dict[str, Any]], avoid: list[float] | None = None) -> list[tuple[float, str]]:
    """(body-local second, text) for each callout at its line's first word, at least min_gap apart from each other and
    from the times in `avoid` (the hook overlay's end, the pattern interrupt)."""
    rows = script.get("callouts") or []
    if not rows or not words or not config()["enabled"]:
        return []
    gap = float(config()["min_gap"])
    counts = [len((script.get("hook") or "").split()), *[len(ln["text"].split()) for ln in script.get("lines") or []]]
    taken = list(avoid or [])
    out = []
    for r in rows:
        idx = int(r["line"])
        wi = sum(counts[: idx + 1])
        if wi >= len(words):
            continue
        t = round(float(words[wi]["start"]), 3)
        if all(abs(t - x) >= gap for x in taken):
            out.append((t, r["text"]))
            taken.append(t)
    return out
