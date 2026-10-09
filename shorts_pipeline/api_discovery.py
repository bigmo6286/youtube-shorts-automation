"""Trend discovery through the official YouTube Data API, so a refresh is not blocked when YouTube rate-limits or
bot-checks yt-dlp on this machine.

Quota (10,000 units a day per Google Cloud project, shared with uploads when no separate API key is set):
  - search.list:    100 units per call  -> budgeted (`discovery.api.searches_per_refresh`), rotating through the
                                            configured hashtags so every topic is covered over a few refreshes
  - videos.list:      1 unit per 50 videos (title, description, tags, duration, views, likes, comments)
  - channels.list:    1 unit per 50 channels (subscribers, uploads playlist)
  - playlistItems:    1 unit per channel (its newest uploads: the "follow the fastest-growing channels" stage)
The API cannot tell a Short from a short regular video, so each kept candidate gets one plain request to
youtube.com/shorts/<id> (a Short answers 200, a regular video redirects to /watch). Transcripts are not available
through the API for other people's videos; yt-dlp still adds them for the top candidates while it is not blocked.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from .config import DATA_DIR, env, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
STATE_PATH = DATA_DIR / "api_discovery.json"
DEFAULTS = {
    "enabled": True,
    "searches_per_refresh": 4,     # 100 quota units each
    "region": "US",
    "language": "en",
    "days": 7,                     # only Shorts published in the last N days ("trending", not all-time hits)
    "transcripts_for_top": 40,     # yt-dlp transcripts for the most-viewed candidates (skipped while blocked)
}


class QuotaExceeded(RuntimeError):
    pass


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("discovery") or {}).get("api")) or {})
    return cfg


def available() -> bool:
    if not config()["enabled"]:
        return False
    if env("YOUTUBE_API_KEY"):
        return True
    from .upload import TOKEN_PATH
    return TOKEN_PATH.exists()


_SERVICE = None


def _get(path: str, **params) -> dict[str, Any]:
    """One API call through the API key when set (its own quota), else the signed-in Google account."""
    global _SERVICE
    key = env("YOUTUBE_API_KEY")
    from .upload import with_retries
    if key:
        def call():
            r = requests.get(f"{API}/{path}", params={**params, "key": key}, timeout=30)
            if r.status_code == 403 and "quota" in r.text.lower():
                raise QuotaExceeded("YouTube API daily quota used up")
            if r.status_code != 200:
                raise RuntimeError(f"YouTube API {path} {r.status_code}: {r.text[:200]}")
            return r.json()
        return with_retries(call, f"YouTube API {path}")
    if _SERVICE is None:
        from .upload import youtube_service
        _SERVICE = youtube_service(interactive=False)
    try:
        return with_retries(lambda: getattr(_SERVICE, path)().list(**params).execute(), f"YouTube API {path}")
    except Exception as exc:  # noqa: BLE001
        if "quotaExceeded" in str(exc) or "exceeded your quota" in str(exc):
            raise QuotaExceeded("YouTube API daily quota used up") from exc
        raise


# ---------------------------------------------------------------------------------------------------- pieces

def _seconds(iso: str) -> int | None:
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def _search_ids(query: str, cfg: dict[str, Any]) -> list[str]:
    after = (datetime.now(timezone.utc) - timedelta(days=int(cfg["days"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = _get("search", part="id", type="video", q=query, videoDuration="short", order="viewCount",
                publishedAfter=after, regionCode=cfg["region"], relevanceLanguage=cfg["language"], maxResults=50,
                safeSearch="moderate")
    return [it["id"]["videoId"] for it in resp.get("items", []) if it.get("id", {}).get("videoId")]


def videos_meta(ids: Iterable[str], source: str) -> list[dict[str, Any]]:
    """Full metadata in the same shape as fetch.fetch_full (transcript added later)."""
    ids = list(dict.fromkeys(ids))
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), 50):
        resp = _get("videos", part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50]), maxResults=50)
        for v in resp.get("items", []):
            sn, st, cd = v.get("snippet", {}), v.get("statistics", {}), v.get("contentDetails", {})
            if sn.get("liveBroadcastContent") not in (None, "none"):
                continue
            published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
            out.append({
                "id": v["id"], "url": f"https://www.youtube.com/shorts/{v['id']}",
                "title": sn.get("title", ""), "description": (sn.get("description") or "")[:1000],
                "duration": _seconds(cd.get("duration", "")),
                "view_count": int(st.get("viewCount", 0) or 0), "like_count": int(st.get("likeCount", 0) or 0),
                "comment_count": int(st.get("commentCount", 0) or 0),
                "channel": sn.get("channelTitle", ""), "channel_id": sn.get("channelId"), "channel_followers": 0,
                "timestamp": published.timestamp(), "upload_date": published.strftime("%Y%m%d"),
                "categories": [], "tags": (sn.get("tags") or [])[:20],
                "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                "width": None, "height": None, "fetched_at": time.time(), "transcript": None, "source": source,
            })
    _add_followers(out)
    return out


def _add_followers(metas: list[dict[str, Any]]) -> None:
    ids = list(dict.fromkeys(m["channel_id"] for m in metas if m.get("channel_id")))
    subs: dict[str, int] = {}
    for i in range(0, len(ids), 50):
        resp = _get("channels", part="statistics", id=",".join(ids[i:i + 50]), maxResults=50)
        for c in resp.get("items", []):
            subs[c["id"]] = int(c.get("statistics", {}).get("subscriberCount", 0) or 0)
    for m in metas:
        m["channel_followers"] = subs.get(m.get("channel_id"), 0)


_SHORT_CHECK: dict[str, bool] = {}


def is_short_url(video_id: str) -> bool | None:
    """True for a Short, False for a regular video, None when YouTube could not be asked."""
    if video_id in _SHORT_CHECK:
        return _SHORT_CHECK[video_id]
    try:
        r = requests.head(f"https://www.youtube.com/shorts/{video_id}", allow_redirects=False, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0"}, cookies={"SOCS": "CAI", "CONSENT": "YES+1"})
    except requests.RequestException:
        return None
    if r.status_code == 200:
        _SHORT_CHECK[video_id] = True
    elif r.status_code in (301, 302, 303, 307, 308) and "/watch" in (r.headers.get("location") or ""):
        _SHORT_CHECK[video_id] = False
    else:
        return None
    return _SHORT_CHECK[video_id]


def _keep_shorts(metas: list[dict[str, Any]], max_duration: int) -> list[dict[str, Any]]:
    metas = [m for m in metas if m.get("duration") and m["duration"] <= max_duration]
    with ThreadPoolExecutor(max_workers=6) as pool:
        verdicts = list(pool.map(lambda m: is_short_url(m["id"]), metas))
    kept = [m for m, ok in zip(metas, verdicts) if ok is not False]    # unknown: keep (duration already fits)
    dropped = sum(1 for ok in verdicts if ok is False)
    if dropped:
        log.info("API discovery: %d short regular videos dropped (not Shorts)", dropped)
    return kept


def _add_transcripts(metas: list[dict[str, Any]], top: int) -> None:
    """yt-dlp transcripts for the most-viewed candidates while it is not blocked; cached metadata is reused."""
    from . import fetch
    cfg = load_config()["discovery"]
    ordered = sorted(metas, key=lambda m: -(m.get("view_count") or 0))[:top]
    added = 0
    for m in ordered:
        cached = fetch._META_CACHE.get(m["id"])
        if cached and cached.get("transcript"):
            m["transcript"] = cached["transcript"]
            continue
        if fetch.rate_limited:
            break
        full = fetch.fetch_full(m["id"], True, cfg.get("transcript_chars", 700))
        if full and full.get("transcript"):
            m["transcript"] = full["transcript"]
            added += 1
    log.info("API discovery: transcripts for %d of the top %d candidates%s", sum(1 for m in ordered if m.get("transcript")),
             len(ordered), " (yt-dlp is blocked right now; the rest are judged on title, description and tags)"
             if fetch.rate_limited else "")
    for m in metas:                                    # keep the judge cache keys stable across runs
        if m.get("transcript"):
            fetch._META_CACHE.set(m["id"], {**(fetch._META_CACHE.get(m["id"]) or {}), **m})


# ---------------------------------------------------------------------------------------------------- stages

def _next_queries(hashtags: list[str], n: int) -> list[str]:
    """Rotate through the configured hashtags so every topic gets searched over a few refreshes."""
    state = load_json(STATE_PATH) or {}
    pos = int(state.get("pos", 0))
    tags = [t.lstrip("#") for t in hashtags if t.lstrip("#").lower() not in ("shorts", "trending", "fyp")] or ["facts"]
    picked = [tags[(pos + i) % len(tags)] for i in range(min(n, len(tags)))]
    state["pos"] = (pos + len(picked)) % len(tags)
    state["last"] = {"at": datetime.now().isoformat(timespec="seconds"), "queries": picked}
    save_json(STATE_PATH, state)
    return [f"#shorts {t}" for t in picked]


def discover(cfg_discovery: dict[str, Any]) -> list[dict[str, Any]]:
    """Recent high-view Shorts for the rotating hashtag searches, plus the most-popular chart, with metadata."""
    cfg = config()
    ids: dict[str, str] = {}
    queries = _next_queries(cfg_discovery.get("hashtags") or [], int(cfg["searches_per_refresh"]))
    for q in queries:
        try:
            for vid in _search_ids(q, cfg):
                ids.setdefault(vid, f"api:search:{q}")
        except QuotaExceeded:
            log.warning("API discovery: daily quota used up; continuing with what was found")
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("API search %r failed: %s", q, str(exc)[:160])
    try:
        chart = _get("videos", part="id", chart="mostPopular", regionCode=cfg["region"], maxResults=50)
        for it in chart.get("items", []):
            ids.setdefault(it["id"], "api:mostPopular")
    except Exception as exc:  # noqa: BLE001
        log.warning("API mostPopular failed: %s", str(exc)[:160])
    metas = videos_meta(ids, "api") if ids else []
    for m in metas:
        m["source"] = ids.get(m["id"], "api")
    shorts = _keep_shorts(metas, int(cfg_discovery.get("max_duration_seconds", 180)))
    _add_transcripts(shorts, int(cfg["transcripts_for_top"]))
    log.info("API discovery: %d Shorts from %d searches (%s) and the most-popular chart; ~%d quota units",
             len(shorts), len(queries), ", ".join(queries), 100 * len(queries) + 3 + len(ids) // 50)
    return shorts


def channel_recent(channel_ids: Iterable[str], per_channel: int, max_duration: int) -> list[dict[str, Any]]:
    """Newest uploads of the given channels (1 unit per channel), kept when they are Shorts."""
    ids = list(dict.fromkeys(c for c in channel_ids if c))
    uploads: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        resp = _get("channels", part="contentDetails", id=",".join(ids[i:i + 50]), maxResults=50)
        for c in resp.get("items", []):
            pl = c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            if pl:
                uploads[c["id"]] = pl
    vids: list[str] = []
    for cid, pl in uploads.items():
        try:
            resp = _get("playlistItems", part="contentDetails", playlistId=pl, maxResults=min(50, per_channel * 3))
            vids += [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        except QuotaExceeded:
            break
        except Exception as exc:  # noqa: BLE001
            log.debug("uploads of %s failed: %s", cid, exc)
    metas = _keep_shorts(videos_meta(vids, "api:channel"), max_duration) if vids else []
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for m in sorted(metas, key=lambda m: -(m.get("timestamp") or 0)):
        by_channel.setdefault(m.get("channel_id") or "", []).append(m)
    out = [m for ms in by_channel.values() for m in ms[:per_channel]]
    log.info("API channel stage: %d recent Shorts from %d channels", len(out), len(uploads))
    return out
