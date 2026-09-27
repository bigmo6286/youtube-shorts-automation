"""Channel feedback: read the public stats of your own uploads and turn them into per-blueprint
performance factors that the scheduler multiplies into its production weights.

Needs YOUTUBE_API_KEY (Data API v3, read-only, no OAuth) and YOUTUBE_CHANNEL (handle like @name, or a
channel id UC...). Uploads are matched to produced Shorts by our upload record or by title.
"""
from __future__ import annotations

import difflib
import logging
import re
import time
from datetime import datetime, timezone
from statistics import median
from typing import Any

import requests

from .config import DATA_DIR, OUTPUT_DIR, env
from .storage import load_json, save_json

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
STATS_PATH = DATA_DIR / "channel_stats.json"
MIN_VIDEOS_PER_BLUEPRINT = 2
FACTOR_MIN, FACTOR_MAX = 0.3, 3.0
MIN_AGE_HOURS = 6              # a video younger than this has no meaningful views-per-hour yet


def configured() -> bool:
    return bool(env("YOUTUBE_API_KEY") and env("YOUTUBE_CHANNEL"))


def _get(path: str, **params) -> dict[str, Any]:
    params["key"] = env("YOUTUBE_API_KEY")
    r = requests.get(f"{API}/{path}", params=params, timeout=30)
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = r.text[:200]
        raise RuntimeError(f"YouTube API {r.status_code}: {msg}")
    return r.json()


def _iso8601_seconds(s: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def resolve_channel(handle_or_id: str) -> dict[str, Any]:
    value = handle_or_id.strip()
    if value.startswith("UC") and len(value) >= 20:
        data = _get("channels", part="snippet,contentDetails,statistics", id=value)
    else:
        data = _get("channels", part="snippet,contentDetails,statistics", forHandle=value.lstrip("@"))
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"channel {value!r} not found")
    ch = items[0]
    return {"id": ch["id"], "title": ch["snippet"]["title"],
            "uploads_playlist": ch["contentDetails"]["relatedPlaylists"]["uploads"],
            "subscribers": int(ch["statistics"].get("subscriberCount", 0)),
            "video_count": int(ch["statistics"].get("videoCount", 0))}


def fetch_uploads(max_videos: int = 200) -> dict[str, Any]:
    """Recent uploads with statistics, Shorts only (<= 180 s). Saved to data/channel_stats.json."""
    if not configured():
        raise RuntimeError("Set YOUTUBE_API_KEY and YOUTUBE_CHANNEL (your @handle) in Settings first.")
    channel = resolve_channel(env("YOUTUBE_CHANNEL"))
    ids: list[str] = []
    token = None
    while len(ids) < max_videos:
        page = _get("playlistItems", part="contentDetails", playlistId=channel["uploads_playlist"], maxResults=50,
                    **({"pageToken": token} if token else {}))
        ids += [it["contentDetails"]["videoId"] for it in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    videos: list[dict[str, Any]] = []
    now = time.time()
    for i in range(0, len(ids), 50):
        batch = _get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
        for v in batch.get("items", []):
            seconds = _iso8601_seconds(v["contentDetails"].get("duration", ""))
            if seconds == 0 or seconds > 180:
                continue
            published = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")).timestamp()
            hours = max(1.0, (now - published) / 3600.0)
            st = v.get("statistics", {})
            views = int(st.get("viewCount", 0))
            videos.append({
                "id": v["id"], "title": v["snippet"]["title"], "published": v["snippet"]["publishedAt"],
                "age_hours": round(hours, 1), "duration": seconds, "views": views,
                "likes": int(st.get("likeCount", 0)), "comments": int(st.get("commentCount", 0)),
                "views_per_hour": round(views / hours, 2),
            })
    data = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "channel": channel, "videos": videos}
    save_json(STATS_PATH, data)
    log.info("channel %s: %d Shorts fetched", channel["title"], len(videos))
    return data


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()


def match_outputs(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach channel stats to produced Shorts: by our upload id, else by (near-)identical title."""
    by_id = {v["id"]: v for v in videos}
    by_title = {_norm(v["title"]): v for v in videos}
    titles = list(by_title)
    matched: list[dict[str, Any]] = []
    if not OUTPUT_DIR.exists():
        return matched
    for d in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        meta = load_json(d / "meta.json")
        if not meta:
            continue
        video = by_id.get(meta.get("youtube_id") or "")
        if not video:
            key = _norm(meta.get("title", ""))
            video = by_title.get(key)
            if not video and key:
                close = difflib.get_close_matches(key, titles, n=1, cutoff=0.85)
                video = by_title[close[0]] if close else None
        if not video:
            continue
        bp = meta.get("blueprint") or {}
        changed = meta.get("youtube_id") != video["id"] or meta.get("channel_stats", {}).get("views") != video["views"]
        meta["youtube_id"] = video["id"]
        meta["channel_stats"] = {k: video[k] for k in ("views", "likes", "comments", "views_per_hour", "age_hours", "published")}
        if changed:
            save_json(d / "meta.json", meta)
        matched.append({"dir": d.name, "title": meta.get("title"), "key": f"{bp.get('format')}|{bp.get('topic')}",
                        "format": bp.get("format"), "topic": bp.get("topic"), **meta["channel_stats"]})
    return matched


def blueprint_performance(matched: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Per-blueprint views/hour relative to the channel median -> factor in [FACTOR_MIN, FACTOR_MAX]."""
    if matched is None:
        data = load_json(STATS_PATH)
        matched = match_outputs(data["videos"]) if data else []
    mature = [m for m in matched if m.get("age_hours", 0) >= MIN_AGE_HOURS]
    if not mature:
        return {"channel_median_vph": None, "videos": len(matched), "blueprints": {}}
    channel_median = median(m["views_per_hour"] for m in mature) or 0.0
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in mature:
        groups.setdefault(m["key"], []).append(m)
    out: dict[str, Any] = {}
    for key, items in groups.items():
        vph = median(i["views_per_hour"] for i in items)
        if channel_median > 0 and len(items) >= MIN_VIDEOS_PER_BLUEPRINT:
            factor = max(FACTOR_MIN, min(FACTOR_MAX, vph / channel_median))
        else:
            factor = 1.0
        out[key] = {"videos": len(items), "median_views_per_hour": round(vph, 2), "median_views": int(median(i["views"] for i in items)),
                    "factor": round(factor, 2), "provisional": len(items) < MIN_VIDEOS_PER_BLUEPRINT}
    return {"channel_median_vph": round(channel_median, 2), "videos": len(mature), "blueprints": out}


def sync() -> dict[str, Any]:
    data = fetch_uploads()
    matched = match_outputs(data["videos"])
    perf = blueprint_performance(matched)
    log.info("channel feedback: %d uploads matched to produced Shorts, %d blueprint keys with data",
             len(matched), len(perf["blueprints"]))
    return {"channel": data["channel"], "fetched_at": data["fetched_at"], "uploads": len(data["videos"]),
            "matched": matched, "performance": perf}


def cached_report() -> dict[str, Any] | None:
    data = load_json(STATS_PATH)
    if not data:
        return None
    matched = match_outputs(data["videos"])
    return {"channel": data["channel"], "fetched_at": data["fetched_at"], "uploads": len(data["videos"]),
            "matched": matched, "performance": blueprint_performance(matched)}
