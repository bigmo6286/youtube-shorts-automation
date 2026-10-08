"""What has already been made, so the engine never makes the same Short twice.

The memory does not depend on output folders (they get deleted to save space) or on which machine made a
video. It is the union of:
  - data/produced_titles.json, appended at every produce (permanent, survives deleted outputs);
  - every upload on the connected channel (data/channel_stats.json, refreshed on each channel sync);
  - whatever output folders still exist.

After a script is drafted, `find_repeat` compares it with that memory. A cheap word-overlap pass picks the
few closest known videos; TypeSafe then judges whether the new one is the same specific subject (same story,
fact, experiment or person, even reworded). Without a TypeSafe key the overlap score decides alone.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR
from .storage import load_json, save_json

log = logging.getLogger(__name__)

HISTORY_PATH = DATA_DIR / "produced_titles.json"
_LOCK = threading.Lock()

SHORTLIST_MIN_OVERLAP = 0.34   # known titles at least this close go to the TypeSafe check
OVERLAP_ALONE = 0.6            # without TypeSafe, this much overlap counts as a repeat
SAME_SUBJECT_NOUL = 0.5

_STOP = set("""a an and are as at be been but by can did do does for from had has have he her his how i if in into
is it its just like more most my no not of on one only or our out over she so than that the their them then there
these they this those to too up us was we were what when where which who why will with you your yours about after
again all also any because before being both could each even ever every few get got here him im ive lets made make
many may might much must never new now off once other own really same should some still such take thing things
think through under until very way well while would yet fact facts shorts short video times time actually""".split())


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+|\d[\d,.]*", (text or "").lower())
    out = set()
    for w in words:
        w = w.replace(",", "").rstrip(".")
        if not w or w in _STOP or (w.isalpha() and len(w) < 3):
            continue
        if w.isalpha() and len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def record(title: str, *, hook: str = "", key: str = "", out_dir: Path | None = None, source: str = "") -> None:
    """Remember a produced Short permanently."""
    if not title:
        return
    with _LOCK:
        data = load_json(HISTORY_PATH) or {"videos": []}
        data["videos"].append({"title": title, "hook": hook[:300], "key": key, "source": source,
                               "dir": out_dir.name if out_dir else "",
                               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        data["videos"] = data["videos"][-3000:]
        save_json(HISTORY_PATH, data)


def known_videos() -> list[dict[str, Any]]:
    """Every video this engine or the channel already has: title, hook (when known), key, when."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def add(title: str, **extra: Any) -> None:
        t = (title or "").strip()
        norm = re.sub(r"\s+", " ", re.sub(r"#\w+", "", t.lower())).strip(" .")
        if not norm or norm in seen:
            return
        seen.add(norm)
        out.append({"title": t, **extra})

    for v in reversed((load_json(HISTORY_PATH) or {}).get("videos", [])):
        add(v["title"], hook=v.get("hook", ""), key=v.get("key", ""), at=v.get("at", ""), origin="produced")
    if OUTPUT_DIR.exists():
        for d in sorted((p for p in OUTPUT_DIR.iterdir() if p.is_dir()), reverse=True):
            meta = load_json(d / "meta.json")
            if meta and meta.get("title"):
                bp = meta.get("blueprint") or {}
                script = load_json(d / "script.json") or {}
                add(meta["title"], hook=script.get("hook", ""), key=f"{bp.get('format')}|{bp.get('topic')}",
                    at=d.name[:19], origin="output")
    try:
        from .channel import labelled_uploads, STATS_PATH
        labels = {u["id"]: u for u in labelled_uploads()}
        for v in sorted((load_json(STATS_PATH) or {}).get("videos", []), key=lambda v: v.get("published", ""), reverse=True):
            lab = labels.get(v["id"], {})
            add(v["title"], hook=(lab.get("transcript") or "")[:200], key=f"{lab.get('format')}|{lab.get('topic')}",
                at=v.get("published", ""), origin="channel")
    except Exception as exc:  # noqa: BLE001
        log.debug("channel uploads unavailable for repeat check: %s", exc)
    return out


def avoid_titles(blueprint: dict[str, Any] | None = None, limit: int = 45) -> list[str]:
    """Titles for the writer's do-not-repeat list: the blueprint's own exemplars from your channel, everything
    already made for the same format x topic, then the most recent videos overall."""
    known = known_videos()
    picked: list[str] = []

    def take(titles: list[str]) -> None:
        for t in titles:
            if t not in picked and len(picked) < limit:
                picked.append(t)

    if blueprint:
        if blueprint.get("source") == "channel":
            take([e.get("title", "") for e in blueprint.get("exemplars") or [] if e.get("title")])
        key = f"{blueprint.get('format')}|{blueprint.get('topic')}"
        take([k["title"] for k in known if k.get("key") == key])
    take([k["title"] for k in known])
    return picked


def _typesafe_same_subject(script: dict[str, Any], candidates: list[dict[str, Any]]) -> list[float] | None:
    from . import judge

    if not judge.has_typesafe():
        return None
    state = {
        "new_video": {"title": script.get("title", ""), "hook": script.get("hook", ""),
                      "script": (script.get("full_text") or "")[:700]},
    }
    questions = {}
    for i, c in enumerate(candidates):
        state[f"previous_video_{i + 1}"] = {"title": c["title"], "opening": (c.get("hook") or "")[:250]}
        questions[f"same_as_{i + 1}"] = judge._q(
            "noul",
            f"Is `new_video` about the same specific subject as `previous_video_{i + 1}`: the same story, event, "
            "experiment, person, animal, place or headline fact, even if it is reworded or framed differently? "
            "Sharing only a broad topic (both about space, both about relationships) does NOT count.",
            None)
    try:
        with judge._client() as client:
            response = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001
        log.warning("repeat check: TypeSafe unavailable (%s); using word overlap only", str(exc)[:120])
        return None
    return [float(response.answers[f"same_as_{i + 1}"].noul) for i in range(len(candidates))]


def find_repeat(script: dict[str, Any], known: list[dict[str, Any]] | None = None) -> str | None:
    """The title of an existing video this script repeats, or None."""
    known = known if known is not None else known_videos()
    text = f"{script.get('title', '')} {script.get('hook', '')}"
    scored = []
    for k in known:
        s = max(overlap(script.get("title", ""), k["title"]), overlap(text, f"{k['title']} {k.get('hook', '')}") * 0.9)
        if s >= SHORTLIST_MIN_OVERLAP:
            scored.append((s, k))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    shortlist = [k for _, k in scored[:5]]
    verdicts = _typesafe_same_subject(script, shortlist)
    if verdicts is None:
        best_score, best = scored[0]
        return best["title"] if best_score >= OVERLAP_ALONE else None
    for v, k in zip(verdicts, shortlist):
        if v >= SAME_SUBJECT_NOUL:
            log.info("repeat check: %r is the same subject as %r (%.0f%%)", script.get("title"), k["title"], v * 100)
            return k["title"]
    return None
