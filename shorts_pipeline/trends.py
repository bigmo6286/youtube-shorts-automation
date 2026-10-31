"""Fast trend reaction: when a topic spikes in search, a Short about it is written, fact-checked and uploaded within hours.

Google's daily search trends feed (free, no key) lists what people in a region are searching for right now, with the
headlines behind each spike. TypeSafe screens every spike for the channel: can a faceless narrated Short explain it
(a story, a discovery, a why, not a sports score or a sale), how risky is it (tragedy, crime, politics, a private
person, health or money advice: stricter than a normal Short), and which of the engine's topic labels it belongs
to. Code keeps the best spike above the thresholds, once per spike, and hands the writer the headlines as its angle.
The Short then goes through the normal fact-check with the strict rule (any unverified main claim holds it),
gets a priority lift in the upload queue and the earliest good publish time, so it is live while people still search.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import requests

from .config import DATA_DIR, has_typesafe, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "checks_per_day": 4, "max_per_day": 1, "region": "US", "min_traffic": 1000,
            "min_fit": 0.7, "max_risk": 0.3, "repeat_days": 14}
# The feed's traffic figures are coarse ("500+", "2K+", "20K+"); a 2K+ spike on it is already a big national story.
STATE_PATH = DATA_DIR / "trends.json"
FEED = "https://trends.google.com/trending/rss?geo={region}"
NS = {"ht": "https://trends.google.com/trending/rss"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; shorts-engine/1.0)"}
MAX_SCREEN = 15


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("schedule") or {}).get("trends")) or {})
    return cfg


def _state() -> dict[str, Any]:
    return load_json(STATE_PATH) or {"made": {}, "last_check": 0.0, "candidates": {}}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")[:50]


def parse_traffic(text: str) -> int:
    """'500+' -> 500, '20K+' -> 20000, '1M+' -> 1000000."""
    m = re.match(r"\s*([\d.,]+)\s*([KkMm]?)", text or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"k": 1000, "m": 1000000}.get(m.group(2).lower(), 1))


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        news = []
        for n in it.findall("ht:news_item", NS):
            news.append({"title": (n.findtext("ht:news_item_title", default="", namespaces=NS) or "").strip(),
                         "snippet": (n.findtext("ht:news_item_snippet", default="", namespaces=NS) or "").strip(),
                         "source": (n.findtext("ht:news_item_source", default="", namespaces=NS) or "").strip(),
                         "url": (n.findtext("ht:news_item_url", default="", namespaces=NS) or "").strip()})
        items.append({"title": title, "traffic": parse_traffic(it.findtext("ht:approx_traffic", default="", namespaces=NS) or ""),
                      "published": (it.findtext("pubDate") or "").strip(), "news": news[:4],
                      "picture": (it.findtext("ht:picture", default="", namespaces=NS) or "").strip()})
    return items


def fetch(region: str | None = None, timeout: float = 20.0) -> list[dict[str, Any]]:
    region = region or config()["region"]
    r = requests.get(FEED.format(region=region), headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return parse_feed(r.text)


def screen(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each spike with fit (0-1), risk (0-1), topic label and total, best first. Without TypeSafe: nothing is chosen."""
    if not items or not has_typesafe():
        return []
    from typesafe_sdk import Choice, Noul, Score, TypeSafeClient
    from .judge import TOPICS
    items = items[:MAX_SCREEN]
    state = {"note": "candidate subjects for a faceless narrated YouTube Short (AI voice over stock footage) on a general-interest channel",
             "spikes": [{"id": i, "search": it["title"], "headlines": [n["title"] for n in it["news"]][:3],
                         "snippets": [n["snippet"] for n in it["news"] if n["snippet"]][:2]} for i, it in enumerate(items)]}
    questions: dict[str, Any] = {}
    for i, _ in enumerate(items):
        questions[f"fit_{i}"] = Score(
            instructions=f"How well could a 40-second narrated explainer about `spikes[{i}]` work for a general audience: is there "
                         "a story, a discovery, a 'why' or a background to explain, that stock footage or photos can show?",
            criteria=["none: a score, a sale, a schedule or a name with nothing to explain",
                      "weak: some background, but mostly for fans or insiders",
                      "good: a clear story or question most people would want explained",
                      "strong: surprising, explainable in a minute, and people are searching because they want to understand it"])
        questions[f"risk_{i}"] = Noul(
            instructions=f"Would a Short about `spikes[{i}]` touch a death, tragedy, crime, war, violence, an election or partisan politics, "
                         "sexual content, a private person's personal life, or health, legal or money advice (anything a cautious "
                         "channel should not react to within hours)?")
        questions[f"topic_{i}"] = Choice(instructions=f"Which topic label fits `spikes[{i}]` best?", criteria=dict(TOPICS))
    with TypeSafeClient(timeout=90.0) as client:
        resp = client.system_one(state=state, questions=questions)
    rows = []
    for i, it in enumerate(items):
        fit = float(resp.answers[f"fit_{i}"].score) / 3
        risk = float(resp.answers[f"risk_{i}"].noul)
        rows.append({**it, "fit": round(fit, 3), "risk": round(risk, 3), "topic": resp.answers[f"topic_{i}"].choice,
                     "total": round(fit * (1 - risk), 3)})
    rows.sort(key=lambda r: -r["total"])
    return rows


def pick(region: str | None = None, now: float | None = None) -> dict[str, Any] | None:
    """The best spike to react to right now, or None. Remembers the chosen spike so the producer can read its headlines."""
    cfg = config()
    now = now or time.time()
    st = _state()
    items = [it for it in fetch(region) if it["traffic"] >= int(cfg["min_traffic"])]
    recent = {k for k, v in st["made"].items() if now - float(v.get("at") or 0) < float(cfg["repeat_days"]) * 86400}
    items = [it for it in items if slug(it["title"]) not in recent]
    if not items:
        log.info("trends: no spike above %s searches that is new to the channel", f"{int(cfg['min_traffic']):,}")
        return None
    ranked = screen(items)
    for r in ranked[:5]:
        log.info("trends: %r %s searches: fit %.2f, risk %.2f, %s", r["title"][:40], f"{r['traffic']:,}", r["fit"], r["risk"], r["topic"])
    good = [r for r in ranked if r["fit"] >= float(cfg["min_fit"]) and r["risk"] <= float(cfg["max_risk"])]
    if not good:
        log.info("trends: %d spikes screened, none fits the channel safely (fit >= %.2f, risk <= %.2f)", len(ranked),
                 float(cfg["min_fit"]), float(cfg["max_risk"]))
        return None
    best = good[0]
    st["candidates"][slug(best["title"])] = {**best, "picked_at": now}
    save_json(STATE_PATH, st)
    return best


def item_for(title: str) -> dict[str, Any]:
    """The remembered spike for a title (from pick), else a fresh look at the feed, else the bare title."""
    st = _state()
    hit = st["candidates"].get(slug(title))
    if hit:
        return hit
    try:
        for it in fetch():
            if slug(it["title"]) == slug(title):
                return it
    except Exception as exc:  # noqa: BLE001
        log.debug("trend feed unavailable: %s", exc)
    return {"title": title, "traffic": 0, "news": [], "topic": None}


def blueprint_for(title: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """(blueprint, angle, item) for `produce --trend`."""
    it = item_for(title)
    topic = it.get("topic") or "other"
    heads = [n["title"] for n in it.get("news") or [] if n.get("title")]
    snippets = [n["snippet"] for n in it.get("news") or [] if n.get("snippet")]
    bp = {"format": "explainer", "topic": topic, "hook_style": "curiosity_gap", "source": "trend",
          "trend": {"title": it["title"], "traffic": it.get("traffic", 0), "headlines": heads[:3],
                    "sources": [n.get("url") for n in it.get("news") or [] if n.get("url")][:3]},
          "why_it_works": f"spiking in search right now ({it.get('traffic', 0):,}+ searches)"}
    angle = (f"People are searching for {it['title']!r} right now. "
             + (("Headlines: " + " | ".join(heads[:3]) + ". ") if heads else "")
             + (("Details: " + " ".join(snippets[:2])[:400] + ". ") if snippets else "")
             + "Explain the story behind the spike: who or what it is, why everyone is suddenly searching, and the one fact "
               "most people do not know. Facts only, from what is confirmed; no speculation, no opinion, no taking sides, "
               "nothing about a private person's life. Open by naming what everyone is searching for.")
    return bp, angle, it


def record_made(title: str, out_dir: str, at: float | None = None) -> None:
    st = _state()
    st["made"][slug(title)] = {"title": title, "at": at or time.time(), "dir": out_dir}
    save_json(STATE_PATH, st)


def made_today(now: float | None = None) -> int:
    now = now or time.time()
    day = datetime.fromtimestamp(now).date()
    return sum(1 for v in _state()["made"].values() if datetime.fromtimestamp(float(v.get("at") or 0)).date() == day)


def maybe_check(now: datetime, submit) -> dict[str, Any] | None:
    """Scheduler hook: every 24/checks_per_day hours, look at the spikes and start a Short for the best one."""
    cfg = config()
    if not cfg["enabled"]:
        return None
    ts = now.timestamp()
    st = _state()
    if ts - float(st.get("last_check") or 0) < 86400 / max(1, int(cfg["checks_per_day"])):
        return None
    if made_today(ts) >= int(cfg["max_per_day"]):
        return None
    st["last_check"] = ts
    save_json(STATE_PATH, st)
    try:
        best = pick(now=ts)
    except Exception as exc:  # noqa: BLE001
        log.warning("trend check failed: %s", str(exc)[:160])
        return None
    if not best:
        return None
    job = submit("produce", {"trend": best["title"], "music": "random", "scheduled": True})
    if job is None:
        st = _state()
        st["last_check"] = 0.0                       # the machine was busy: look again on the next tick
        save_json(STATE_PATH, st)
        return None
    record_made(best["title"], f"job:{job.get('id')}", at=ts)
    log.info("trend reaction: %r (%s searches, fit %.2f, risk %.2f) -> job %s", best["title"], f"{best['traffic']:,}",
             best["fit"], best["risk"], job.get("id"))
    return job
