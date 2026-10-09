"""Second-chance titles for Shorts that underperform.

`after_hours` (48 h) after a Short went public, if its views per hour are in the bottom third of the channel's recent
public Shorts, its title is changed once to the best alternative: a search-based variant saved when it was produced
(titles.py: TypeSafe judged it accurate and natural), or, when none was saved, fresh variants judged the same way.
Another `after_hours` later the engine compares views per hour before and after and keeps a tally, so you can see
whether second titles help on this channel (Overview and daily report).
"""
from __future__ import annotations

import logging
import statistics as st
import time
from datetime import datetime
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "after_hours": 48, "bottom_share": 0.33, "max_per_sync": 2, "min_accuracy": 0.6,
            "min_natural": 0.5, "improved_factor": 1.2}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("retitle")) or {})
    return cfg


def _public_since(meta: dict[str, Any], stats: dict[str, Any]) -> float | None:
    if stats.get("published"):
        return datetime.fromisoformat(stats["published"].replace("Z", "+00:00")).timestamp()
    return meta.get("uploaded_at")


def _alternative(d, meta: dict[str, Any]) -> str | None:
    """Best saved variant other than the current title, else fresh ones (TypeSafe-judged)."""
    cfg = config()
    script = load_json(d / "script.json") or {}
    current = meta["title"].strip().lower()
    saved = [c for c in (script.get("title_search") or {}).get("candidates", [])
             if c["title"].strip().lower() != current and c.get("accurate", 0) >= cfg["min_accuracy"]
             and c.get("natural", 1.0) >= cfg["min_natural"]]
    if saved:
        return max(saved, key=lambda c: c.get("tap", 0))["title"]
    if not script.get("full_text"):
        return None
    from . import titles
    phrases = titles.search_phrases({**script, "title": meta["title"]})
    variants = titles._variants({**script, "title": meta["title"]}, phrases, 3) if phrases else []
    if not variants:
        return None
    best, details = titles.pick({**script, "title": meta["title"]}, [meta["title"]] + variants, phrases)
    ok = [c for c in details.get("candidates", [])[1:]
          if c["accurate"] >= cfg["min_accuracy"] and c.get("natural", 1.0) >= cfg["min_natural"]]
    return max(ok, key=lambda c: c["tap"])["title"] if ok else None


def _set_title(video_id: str, title: str) -> None:
    from .upload import with_retries, youtube_service
    yt = with_retries(lambda: youtube_service(interactive=False), "connect to YouTube")
    item = with_retries(lambda: yt.videos().list(part="snippet", id=video_id).execute(), "read title")["items"][0]
    snippet = item["snippet"]
    body = {"id": video_id, "snippet": {"title": title[:100], "categoryId": snippet.get("categoryId", "22"),
                                         "description": snippet.get("description", ""), "tags": snippet.get("tags", [])}}
    if snippet.get("defaultLanguage"):
        body["snippet"]["defaultLanguage"] = snippet["defaultLanguage"]
    with_retries(lambda: yt.videos().update(part="snippet", body=body).execute(), "change title")


def run() -> dict[str, Any]:
    """Change weak Shorts' titles once, and judge earlier changes. Called on every channel sync."""
    cfg = config()
    out = {"retitled": [], "judged": 0}
    if not cfg["enabled"] or not OUTPUT_DIR.exists():
        return out
    stats = {v["id"]: v for v in (load_json(DATA_DIR / "channel_stats.json") or {}).get("videos", [])}
    now = time.time()
    recent = [v for v in stats.values() if v.get("privacy", "public") == "public" and 48 <= float(v.get("age_hours") or 0) <= 14 * 24]
    if len(recent) < 9:
        return out
    cutoff = sorted(float(v["views_per_hour"]) for v in recent)[max(0, int(len(recent) * float(cfg["bottom_share"])) - 1)]
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if not meta or not meta.get("youtube_id") or meta.get("privacy") != "public":
            continue
        s = stats.get(meta["youtube_id"])
        if not s:
            continue
        rt = meta.get("retitle")
        if rt and not rt.get("verdict") and now - rt["at"] >= float(cfg["after_hours"]) * 3600:
            after = max(0, int(s.get("views", 0)) - int(rt["views_before"])) / max(1.0, (now - rt["at"]) / 3600)
            rt.update(vph_after=round(after, 3),
                      verdict="improved" if after >= float(cfg["improved_factor"]) * max(0.001, rt["vph_before"]) else "no change")
            save_json(d / "meta.json", meta)
            out["judged"] += 1
            continue
        if rt or len(out["retitled"]) >= int(cfg["max_per_sync"]):
            continue
        since = _public_since(meta, s)
        if not since or now - since < float(cfg["after_hours"]) * 3600 or float(s["views_per_hour"]) > cutoff:
            continue
        new = _alternative(d, meta)
        if not new:
            continue
        try:
            _set_title(meta["youtube_id"], new)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not change the title of %s: %s", meta["youtube_id"], str(exc)[:160])
            continue
        meta = load_json(d / "meta.json") or meta
        meta["retitle"] = {"at": now, "old": meta["title"], "new": new, "views_before": int(s.get("views", 0)),
                           "vph_before": float(s.get("views_per_hour") or 0)}
        meta["title"] = new
        save_json(d / "meta.json", meta)
        out["retitled"].append((meta["retitle"]["old"], new))
        log.info("second-chance title: %r -> %r (%.2f views/h, bottom third)", meta["retitle"]["old"], new, s["views_per_hour"])
    return out


def tally() -> dict[str, int]:
    t = {"tried": 0, "improved": 0, "no_change": 0, "pending": 0}
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            rt = (load_json(d / "meta.json") or {}).get("retitle") if d.is_dir() else None
            if rt:
                t["tried"] += 1
                t["improved" if rt.get("verdict") == "improved" else "no_change" if rt.get("verdict") else "pending"] += 1
    return t
