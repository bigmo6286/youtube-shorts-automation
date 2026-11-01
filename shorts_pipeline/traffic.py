"""Traffic sources from YouTube Analytics (free, read-only, same login as retention): where the views come from.

Three things the channel can act on, refreshed at every channel sync into data/traffic.json:
  - the real search terms that brought viewers (YT_SEARCH detail): proven phrases for the title picker and the
    demand notes the writer sees;
  - the videos YouTube suggested this channel's Shorts after (RELATED_VIDEO detail) and what they were about;
  - each recent Short's split between the Shorts feed, search and suggested, so a post-mortem can tell "the feed
    never picked it up" from "viewers did not like it", and a channel-wide watch on daily Shorts-feed views that
    alerts on Telegram when the feed stops pushing the channel.
Analytics lags about two days, so the windows end the day before yesterday.
"""
from __future__ import annotations

import logging
import re
import statistics as st
import time
from datetime import date, datetime, timedelta
from typing import Any

from .config import DATA_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

TRAFFIC_PATH = DATA_DIR / "traffic.json"
DEFAULTS = {"enabled": True, "days": 28, "per_video": 8, "feed_drop_factor": 0.35, "min_daily_feed": 50}
LAG_DAYS = 2
FEED_DAYS = 21
NAMES = {"SHORTS": "Shorts feed", "YT_SEARCH": "YouTube search", "RELATED_VIDEO": "suggested videos",
         "SUBSCRIBER": "subscriptions feed", "NOTIFICATION": "notifications", "EXT_URL": "external links",
         "NO_LINK_OTHER": "direct or unknown", "YT_CHANNEL": "channel page", "YT_OTHER_PAGE": "other YouTube pages",
         "PLAYLIST": "playlists", "YT_PLAYLIST_PAGE": "playlist pages", "HASHTAGS": "hashtag pages", "SOUND_PAGE": "sound pages",
         "END_SCREEN": "end screens", "ADVERTISING": "ads", "PROMOTED": "promoted", "ANNOTATION": "annotations",
         "CAMPAIGN_CARD": "cards", "SHORTS_CONTENT_LINKS": "links in Shorts", "VIDEO_REMIXES": "remixes",
         "LIVE_REDIRECT": "live redirects", "IMMERSIVE_LIVE": "immersive live", "PRODUCT_PAGE": "product pages"}
_STOP = set("the a an and or of to in on at for with from by is are was were be been it its this that these those your you "
            "how why what when where who does do did can could would should will about into than then there here".split())


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("schedule") or {}).get("traffic")) or {})
    return cfg


def cached() -> dict[str, Any]:
    return load_json(TRAFFIC_PATH) or {}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z][a-z'-]+", (text or "").lower()) if w not in _STOP and len(w) > 2}


# ---------------------------------------------------------------------------------------------------- fetching

def _query(yt, what: str, **kw) -> list[list[Any]]:
    from .upload import with_retries
    resp = with_retries(lambda: yt.reports().query(ids="channel==MINE", **kw).execute(), what)
    return resp.get("rows") or []


def _recent_uploads(limit: int) -> list[dict[str, Any]]:
    data = load_json(DATA_DIR / "channel_stats.json") or {}
    vids = [v for v in data.get("videos") or [] if v.get("privacy", "public") == "public" and v.get("id")]
    vids.sort(key=lambda v: float(v.get("published_ts") or 0), reverse=True)
    return vids[:limit]


def _titles_for(ids: list[str]) -> dict[str, dict[str, str]]:
    """Title and channel of other videos (Data API videos.list, 1 unit per 50 ids). Never raises."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    try:
        from .upload import youtube_service, with_retries
        yt = youtube_service(interactive=False)
        resp = with_retries(lambda: yt.videos().list(part="snippet", id=",".join(ids[:50])).execute(), "look up suggested videos")
        return {it["id"]: {"title": it["snippet"].get("title", ""), "channel": it["snippet"].get("channelTitle", "")}
                for it in resp.get("items") or []}
    except Exception as exc:  # noqa: BLE001
        log.debug("suggested-video titles not fetched: %s", str(exc)[:120])
        return {}


def fetch(days: int | None = None, per_video: int | None = None) -> dict[str, Any]:
    """Read the traffic reports and save them. Raises when Analytics is not reachable."""
    from .analytics import _service
    cfg = config()
    days = int(days or cfg["days"])
    per_video = int(cfg["per_video"] if per_video is None else per_video)
    yt = _service()
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=days)
    prev = cached()
    data: dict[str, Any] = {"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"), "start": start.isoformat(),
                            "end": end.isoformat(), "days": days}
    rows = _query(yt, "read traffic sources", startDate=start.isoformat(), endDate=end.isoformat(), metrics="views",
                  dimensions="insightTrafficSourceType", sort="-views")
    data["sources"] = {str(r[0]): int(r[1]) for r in rows}
    rows = _query(yt, "read search terms", startDate=start.isoformat(), endDate=end.isoformat(), metrics="views",
                  dimensions="insightTrafficSourceDetail", filters="insightTrafficSourceType==YT_SEARCH", sort="-views", maxResults=25)
    data["search_terms"] = [{"term": str(r[0]).strip().lower(), "views": int(r[1])} for r in rows if str(r[0]).strip()]
    rows = _query(yt, "read suggested-video sources", startDate=start.isoformat(), endDate=end.isoformat(), metrics="views",
                  dimensions="insightTrafficSourceDetail", filters="insightTrafficSourceType==RELATED_VIDEO", sort="-views", maxResults=25)
    sugg = [{"video_id": str(r[0]), "views": int(r[1])} for r in rows]
    known = {s["video_id"]: s for s in (prev.get("suggested_from") or []) if s.get("title")}
    need = [s["video_id"] for s in sugg if s["video_id"] not in known]
    found = _titles_for(need)
    for s in sugg:
        info = known.get(s["video_id"]) or found.get(s["video_id"]) or {}
        s["title"], s["channel"] = info.get("title", ""), info.get("channel", "")
    data["suggested_from"] = sugg
    fstart = end - timedelta(days=FEED_DAYS)
    rows = _query(yt, "read daily Shorts-feed views", startDate=fstart.isoformat(), endDate=end.isoformat(), metrics="views",
                  dimensions="day", filters="insightTrafficSourceType==SHORTS", sort="day")
    data["feed_daily"] = [{"day": str(r[0]), "views": int(r[1])} for r in rows]
    # per-video split for the newest public uploads: one call each, refreshed weekly while the video is young
    per: dict[str, Any] = dict(prev.get("per_video") or {})
    now = time.time()
    done = 0
    for v in _recent_uploads(40):
        if done >= per_video:
            break
        have = per.get(v["id"])
        age_h = float(v.get("age_hours") or 0)
        if have and (age_h > 21 * 24 or now - float(have.get("at") or 0) < 7 * 86400):
            continue
        try:
            rows = _query(yt, "read a video's traffic sources", startDate=(end - timedelta(days=90)).isoformat(), endDate=end.isoformat(),
                          metrics="views", dimensions="insightTrafficSourceType", filters=f"video=={v['id']}", sort="-views")
        except Exception as exc:  # noqa: BLE001
            log.warning("traffic split for %s failed: %s", v["id"], str(exc)[:120])
            break
        split = {str(r[0]): int(r[1]) for r in rows}
        per[v["id"]] = {"sources": split, "views": sum(split.values()), "at": now}
        done += 1
    data["per_video"] = per
    save_json(TRAFFIC_PATH, data)
    log.info("traffic sources: %d search terms, %d suggesting videos, %d video splits (%s to %s)", len(data["search_terms"]),
             len(sugg), done, start, end)
    return data


# ---------------------------------------------------------------------------------------------------- reading

def search_terms(limit: int = 25) -> list[dict[str, Any]]:
    return list(cached().get("search_terms") or [])[:limit]


def terms_for(topic: str, limit: int = 8) -> list[str]:
    """Search terms that brought viewers and share a content word with `topic` (a topic name, a title, subject words)."""
    core = _words(topic)
    if not core:
        return []
    out = []
    for t in search_terms():
        if _words(t["term"]) & core and t["term"] not in out:
            out.append(t["term"])
    return out[:limit]


def shares(sources: dict[str, int] | None = None) -> dict[str, float]:
    src = sources if sources is not None else (cached().get("sources") or {})
    total = sum(src.values()) or 0
    if not total:
        return {}
    return {k: round(v / total, 3) for k, v in src.items()}


def split_for(video_id: str) -> dict[str, Any] | None:
    """One Short's feed / search / suggested shares against the channel's, for the post-mortem."""
    rec = (cached().get("per_video") or {}).get(video_id)
    if not rec or not rec.get("views"):
        return None
    mine, chan = shares(rec["sources"]), shares()
    out = {"views": rec["views"]}
    for key, name in (("SHORTS", "feed"), ("YT_SEARCH", "search"), ("RELATED_VIDEO", "suggested")):
        out[name] = mine.get(key, 0.0)
        if chan:
            out[f"channel_{name}"] = chan.get(key, 0.0)
    return out


def feed_check(data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A drop in daily Shorts-feed views: the last 3 complete days against the 14 before. None when nothing is wrong."""
    cfg = config()
    data = data if data is not None else cached()
    days = [int(d["views"]) for d in data.get("feed_daily") or []]
    if len(days) < 10:
        return None
    recent, before = days[-3:], days[-17:-3]
    if len(before) < 7:
        return None
    med_before, med_recent = st.median(before), st.median(recent)
    if med_before < float(cfg["min_daily_feed"]):
        return None
    if med_recent <= float(cfg["feed_drop_factor"]) * med_before:
        return {"recent": med_recent, "before": med_before,
                "text": (f"The Shorts feed has stopped pushing the channel: about {med_recent:,.0f} feed views a day over the last "
                         f"3 days against {med_before:,.0f} before. Nothing to fix in the videos; check the newest uploads are "
                         "public and not restricted, and keep the schedule steady.")}
    return None


def overview() -> dict[str, Any] | None:
    """For the console's channel panel."""
    data = cached()
    if not data:
        return None
    sh = shares()
    return {"fetched_at": data.get("fetched_at"), "days": data.get("days"),
            "shares": [{"source": NAMES.get(k, k.lower()), "share": v} for k, v in sorted(sh.items(), key=lambda kv: -kv[1])[:5]],
            "search_terms": [t["term"] for t in (data.get("search_terms") or [])[:8]],
            "suggested_from": [{"title": s.get("title") or s["video_id"], "channel": s.get("channel", ""), "views": s["views"]}
                               for s in (data.get("suggested_from") or [])[:5]],
            "feed_drop": feed_check(data)}


def summary_text() -> str:
    """One line for the daily report, or ''."""
    data = cached()
    sh = shares()
    if not sh:
        return ""
    top = ", ".join(f"{NAMES.get(k, k.lower())} {v:.0%}" for k, v in sorted(sh.items(), key=lambda kv: -kv[1])[:3])
    terms = [t["term"] for t in (data.get("search_terms") or [])[:3]]
    return f"Traffic ({data.get('days', 28)} d): {top}" + (f" · top searches: {', '.join(terms)}" if terms else "")


def report_text() -> str:
    """For Telegram /traffic."""
    data = cached()
    if not data:
        return "No traffic data yet: connect YouTube Analytics and run a channel sync."
    sh = shares()
    lines = [f"Where views came from ({data.get('start')} to {data.get('end')}):"]
    lines += [f"  {NAMES.get(k, k.lower())}: {v:.0%}" for k, v in sorted(sh.items(), key=lambda kv: -kv[1])[:6]]
    terms = data.get("search_terms") or []
    if terms:
        lines.append("Search terms that found the channel:")
        lines += [f"  {t['views']:>4}  {t['term']}" for t in terms[:8]]
    sugg = data.get("suggested_from") or []
    if sugg:
        lines.append("Suggested next to:")
        lines += [f"  {s['views']:>4}  {(s.get('title') or s['video_id'])[:50]}" + (f" ({s['channel']})" if s.get("channel") else "")
                  for s in sugg[:5]]
    drop = feed_check(data)
    if drop:
        lines.append("⚠️ " + drop["text"])
    return "\n".join(lines)


def sync() -> str:
    """Channel-sync hook: refresh, then alert once a day if the feed dropped. Returns a status line."""
    cfg = config()
    if not cfg["enabled"]:
        return "traffic sources: off"
    from .analytics import connected
    if not connected():
        return "traffic sources: YouTube Analytics not connected"
    try:
        data = fetch()
    except Exception as exc:  # noqa: BLE001
        return f"traffic sources: not fetched ({str(exc)[:120]})"
    drop = feed_check(data)
    if drop:
        from .alerts import send
        send("feed_drop", "📉 " + drop["text"], repeat_hours=24)
    sh = shares()
    top = ", ".join(f"{NAMES.get(k, k.lower())} {v:.0%}" for k, v in sorted(sh.items(), key=lambda kv: -kv[1])[:3])
    return f"traffic sources: {top}; {len(data['search_terms'])} search terms, {len(data['suggested_from'])} suggesting videos" + \
        ("; FEED DROP" if drop else "")
