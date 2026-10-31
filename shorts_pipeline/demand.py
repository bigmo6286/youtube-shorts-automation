"""Demand-driven subjects: what people actually ask about a topic.

YouTube and Google autocomplete, seeded with question starters ("why do", "how does", "what happens if", "is it true",
"<topic> facts"), and Reddit's public search give the phrases people type and the questions they post. TypeSafe keeps
the ones that are a real curiosity someone has (not a product, a how-to-buy or noise) and that one Short could answer.
The result feeds the writer ("questions people are actually asking") and the subject screening. Cached per topic per day.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from .config import has_typesafe
from .storage import JsonCache

log = logging.getLogger(__name__)

_CACHE = JsonCache("demand")
TTL = 86400
STARTERS = ["why do", "why does", "how does", "how did", "what happens if", "what happens when", "is it true that",
            "did you know", "facts about", "the truth about", "why can't", "how long does"]
MAX_QUESTIONS = 12
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ShortsEngine/1.0"}


def _autocomplete(query: str, ds: str | None) -> list[str]:
    params = {"client": "firefox", "hl": "en", "gl": "US", "q": query}
    if ds:
        params["ds"] = ds
    try:
        r = requests.get("https://suggestqueries.google.com/complete/search", params=params, timeout=10, headers=UA)
        r.raise_for_status()
        data = r.json()
        return [s for s in (data[1] if len(data) > 1 else []) if isinstance(s, str)]
    except Exception as exc:  # noqa: BLE001
        log.debug("autocomplete %r failed: %s", query, exc)
        return []


def _reddit(topic: str, limit: int = 25) -> list[str]:
    """Titles of popular question posts about the topic (public JSON; Reddit may refuse, then nothing)."""
    try:
        r = requests.get("https://www.reddit.com/search.json", params={"q": f"{topic} ?", "sort": "top", "t": "year", "limit": limit},
                         headers={"User-Agent": "ShortsEngine/1.0 (research of common questions; contact via github.com/bigmo6286)"}, timeout=15)
        if r.status_code != 200:
            return []
        posts = r.json().get("data", {}).get("children", [])
        return [p["data"]["title"] for p in posts if "?" in p.get("data", {}).get("title", "")]
    except Exception as exc:  # noqa: BLE001
        log.debug("reddit search failed: %s", exc)
        return []


def raw_phrases(topic: str) -> list[dict[str, Any]]:
    """Everything the sources say people ask about the topic, with where it came from."""
    topic = re.sub(r"\s+", " ", topic.strip().lower())
    seen: dict[str, dict[str, Any]] = {}

    def add(text: str, source: str) -> None:
        t = re.sub(r"\s+", " ", text.strip().lower().rstrip("?")).strip()
        if len(t.split()) < 3 or len(t) > 120:
            return
        if t not in seen:
            seen[t] = {"text": t, "sources": set()}
        seen[t]["sources"].add(source)

    for starter in STARTERS:
        q = f"{starter} {topic}" if not starter.endswith("about") else f"{starter} {topic}"
        for s in _autocomplete(q, "yt"):
            add(s, "youtube")
        for s in _autocomplete(q, None):
            add(s, "google")
    for title in _reddit(topic):
        add(title, "reddit")
    rows = [{"text": v["text"], "sources": sorted(v["sources"])} for v in seen.values()]
    rows.sort(key=lambda r: -len(r["sources"]))
    return rows[:60]


def questions(topic: str, limit: int = MAX_QUESTIONS) -> list[dict[str, Any]]:
    """The phrases that are real curiosities one Short could answer, best-supported first."""
    key = topic.strip().lower()
    hit = _CACHE.get(key)
    if hit and time.time() - float(hit.get("at") or 0) < TTL:
        return hit["questions"][:limit]
    rows = raw_phrases(topic)
    if not rows:
        _CACHE.set(key, {"at": time.time(), "questions": []})
        return []
    keep = rows
    if has_typesafe() and rows:
        try:
            keep = _filter(topic, rows)
        except Exception as exc:  # noqa: BLE001
            log.warning("demand filter failed (%s); using the raw phrases", str(exc)[:100])
    out = keep[:limit]
    _CACHE.set(key, {"at": time.time(), "questions": out})
    return out


def _filter(topic: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from typesafe_sdk import Noul, TypeSafeClient
    rows = rows[:40]
    state = {"topic": topic, "phrases": [{"id": i, "text": r["text"]} for i, r in enumerate(rows)]}
    questions_ = {f"q_{i}": Noul(instructions=(
        f"Is `phrases[{i}].text` a genuine curiosity people have about `topic` that one short narrated video could answer "
        "with facts (not a shopping, pricing, how-to-buy, login, download or app query, not a request for a list of "
        "products, and not noise)?")) for i in range(len(rows))}
    with TypeSafeClient(timeout=60.0) as client:
        resp = client.system_one(state=state, questions=questions_)
    scored = []
    for i, r in enumerate(rows):
        p = float(resp.answers[f"q_{i}"].noul)
        if p >= 0.6:
            scored.append((p + 0.1 * len(r["sources"]), {**r, "fit": round(p, 2)}))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


def prompt_note(topic: str, limit: int = 8) -> str:
    qs = questions(topic, limit)
    if not qs:
        return ""
    return ("\nQuestions people are actually asking about this topic (search autocomplete and Reddit). A subject that answers "
            "one of these has proven demand; prefer it when it fits:\n" + "\n".join(f"- {q['text']}?" for q in qs) + "\n")
