"""Search-driven titles: phrases people actually type into YouTube, and TypeSafe picks the title.

1. Seed queries come from the script's subject (two-word phrases, never single words: "frog" suggests channel names).
2. YouTube's free search-suggest endpoint (no key) returns what people type after each seed.
3. The script writer turns the original title into a few variants that each contain one of those phrases naturally,
   without changing what the video is about.
4. TypeSafe judges which title a Shorts viewer is most likely to tap (a Choice over the candidates) and whether each
   one is accurate to the script (a Noul per candidate). Code keeps the original unless a variant is accurate and
   clearly preferred.
Any failure keeps the original title.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

from .config import load_config

log = logging.getLogger(__name__)

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
DEFAULTS = {"enabled": True, "variants": 3, "region": "US", "language": "en", "min_accuracy": 0.6, "min_margin": 0.05}
_STOP = set("""the a an and or of to in on at for with from by is are was were be been it its this that these those your you
how why what when who which more than then just only even ever every one two three five six seven many most some into out
about after before over under again new really actually sound sounds fake true facts fact thing things way""".split())


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("search_titles")) or {})
    return cfg


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z'-]+", (text or "").lower()) if w not in _STOP and len(w) > 2]


def seed_queries(script: dict[str, Any], n: int = 4) -> list[str]:
    """Two-word search seeds about the video's subject."""
    subject = _words(script.get("visual_fallback", ""))
    title = _words(script.get("title", ""))
    thumb = _words(script.get("thumbnail_text", ""))
    seeds: list[str] = []
    if len(subject) >= 2:
        seeds.append(" ".join(subject[:2]))
    main = (subject or title or thumb)[:1]
    for w in title + thumb:
        if main and w != main[0]:
            seeds.append(f"{main[0]} {w}")
    if main:
        seeds.append(f"why {main[0]}")
    out: list[str] = []
    for s in seeds:
        if s not in out:
            out.append(s)
    return out[:n]


def suggestions(query: str, cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or config()
    try:
        r = requests.get(SUGGEST_URL, params={"client": "firefox", "ds": "yt", "hl": cfg["language"], "gl": cfg["region"],
                                              "q": query}, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        return [s for s in (data[1] if len(data) > 1 else []) if isinstance(s, str)]
    except Exception as exc:  # noqa: BLE001
        log.debug("search suggestions for %r failed: %s", query, exc)
        return []


def search_phrases(script: dict[str, Any], limit: int = 20) -> list[str]:
    cfg = config()
    seen: list[str] = []
    for q in seed_queries(script):
        for s in suggestions(q, cfg):
            s = s.strip().lower()
            if s and s not in seen and len(s.split()) >= 2:
                seen.append(s)
    return seen[:limit]


def _variants(script: dict[str, Any], phrases: list[str], n: int) -> list[str]:
    """New titles from the script writer, each built around a real search phrase."""
    from . import script_gen
    system = ("You rewrite YouTube Shorts titles so they match what people actually search for, without changing what "
              "the video is about. Output only the JSON object.")
    prompt = (f"Current title: {script['title']!r}\nOpening line: {script.get('hook', '')!r}\n"
              f"Script: {script.get('full_text', '')[:900]!r}\n\n"
              f"Phrases people type into YouTube search about this subject (from YouTube's own suggestions):\n"
              + "\n".join(f"- {p}" for p in phrases) +
              f"\n\nWrite {n} alternative titles. Each one must naturally contain one of those phrases (exactly or nearly), "
              "keep the curiosity of the current title, stay 100% accurate to the script (no promise the video does not "
              "keep), be 40-70 characters, and have no hashtags or emojis. Skip phrases that are about something else.")
    schema = {"type": "object", "properties": {"titles": {"type": "array", "items": {
        "type": "object", "properties": {"title": {"type": "string"}, "search_phrase": {"type": "string"}},
        "required": ["title", "search_phrase"]}}}, "required": ["titles"]}
    backend = script_gen.pick_backend(load_config()["production"].get("script_backend", "auto"))
    if backend == "ollama":
        from . import ollama_backend
        data = ollama_backend.generate_json(system, prompt, schema)
    elif backend == "api":
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(model=script_gen.MODEL, max_tokens=800, system=system + " Reply with JSON only.",
                                      messages=[{"role": "user", "content": prompt}])
        import json
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    else:
        from . import claude_code_backend
        data = claude_code_backend.generate_json(system, prompt, schema)
    out = []
    for t in (data.get("titles") or [])[:n]:
        title = re.sub(r"#\w+", "", str(t.get("title") or "")).strip()
        if title and title.lower() != script["title"].lower() and title not in out:
            out.append(title)
    return out


def pick(script: dict[str, Any], candidates: list[str], phrases: list[str]) -> tuple[int, dict[str, Any]]:
    """TypeSafe: which title gets tapped (Choice), and is each one accurate (Noul). Returns (index, details)."""
    from . import judge
    if not judge.has_typesafe() or len(candidates) < 2:
        return 0, {}
    letters = [chr(ord("A") + i) for i in range(len(candidates))]
    questions = {
        "best": judge._q("choice", {
            "question": "A viewer sees this title under a Short in YouTube search and in the Shorts feed. Which title "
                        "are they most likely to tap, given what they search for (`search_phrases`)?",
            "focus": "Prefer specific, curiosity-building titles that use words people search; penalise vague or "
                     "keyword-stuffed ones.",
        }, {letter: title for letter, title in zip(letters, candidates)}),
    }
    for i, title in enumerate(candidates):
        questions[f"accurate_{i}"] = judge._q(
            "noul", f"Does the title {title!r} describe `video.script` accurately, promising nothing the script does not deliver?",
            None)
    state = {"video": {"opening_line": script.get("hook", ""), "script": (script.get("full_text") or "")[:1200]},
             "search_phrases": phrases[:15]}
    with judge._client() as client:
        r = client.system_one(state=state, questions=questions)
    probs = {k: float(v) for k, v in r.answers["best"].probabilities.items()}
    accurate = [float(r.answers[f"accurate_{i}"].noul) for i in range(len(candidates))]
    details = {"candidates": [{"title": t, "tap": round(probs.get(letters[i], 0.0), 3), "accurate": round(accurate[i], 3)}
                              for i, t in enumerate(candidates)]}
    cfg = config()
    ok = [i for i in range(len(candidates)) if accurate[i] >= float(cfg["min_accuracy"])]
    if 0 not in ok:
        ok.append(0)                        # the original stays eligible: it already passed the script QA
    best = max(ok, key=lambda i: probs.get(letters[i], 0.0))
    if best != 0 and probs.get(letters[best], 0) - probs.get(letters[0], 0) < float(cfg["min_margin"]):
        best = 0                            # a coin flip is not worth changing the title
    return best, details


def improve(script: dict[str, Any]) -> dict[str, Any]:
    """Possibly replace script['title'] with a search-driven variant. Never raises."""
    cfg = config()
    if not cfg["enabled"] or not script.get("title"):
        return script
    try:
        phrases = search_phrases(script)
        if not phrases:
            log.info("search titles: no suggestions for this subject; keeping %r", script["title"])
            return script
        variants = _variants(script, phrases, int(cfg["variants"]))
        candidates = [script["title"]] + variants
        best, details = pick(script, candidates, phrases)
        script["title_search"] = {"phrases": phrases, **details, "chosen": candidates[best], "original": script["title"]}
        if best:
            from .script_gen import TITLE_MAX, _shorten
            log.info("search titles: %r -> %r (uses what people search: %s)", script["title"], candidates[best],
                     ", ".join(phrases[:4]))
            script["title"] = _shorten(candidates[best], TITLE_MAX)
        else:
            log.info("search titles: kept %r over %d search-based variants", script["title"], len(variants))
    except Exception as exc:  # noqa: BLE001 - the original title is fine
        log.warning("search titles skipped: %s", str(exc)[:160])
    return script
