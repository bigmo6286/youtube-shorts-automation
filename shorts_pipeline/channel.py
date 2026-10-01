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
MIN_VIDEOS_PER_BLUEPRINT = 1   # one video already counts, shrunk toward neutral until more arrive
FACTOR_MIN, FACTOR_MAX = 0.2, 4.0
MIN_AGE_HOURS = 3              # a video younger than this has no meaningful views-per-hour yet


def _oauth() -> bool:
    from .upload import oauth_available
    return oauth_available()


def configured() -> bool:
    """OAuth client (reads *your* channel, no handle needed) or API key + channel handle."""
    return _oauth() or bool(env("YOUTUBE_API_KEY") and env("YOUTUBE_CHANNEL"))


_SERVICE = None


def _get(path: str, **params) -> dict[str, Any]:
    """channels / playlistItems / videos list call, through OAuth when available, else the API key."""
    global _SERVICE
    last: Exception | None = None
    for attempt in range(3):                       # transient TLS / connection hiccups are common on flaky links
        try:
            if _oauth():
                if _SERVICE is None:
                    from .upload import youtube_service
                    _SERVICE = youtube_service(interactive=True)
                return getattr(_SERVICE, path)().list(**params).execute()
            r = requests.get(f"{API}/{path}", params={**params, "key": env("YOUTUBE_API_KEY")}, timeout=30)
            if r.status_code != 200:
                try:
                    msg = r.json()["error"]["message"]
                except Exception:  # noqa: BLE001
                    msg = r.text[:200]
                raise RuntimeError(f"YouTube API {r.status_code}: {msg}")
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            text = str(exc)
            transient = any(k in text for k in ("EOF", "SSL", "Connection", "timed out", "Timeout", "503", "500", "RemoteDisconnected"))
            if not transient or attempt == 2:
                break
            _SERVICE = None                        # rebuild the HTTP session before retrying
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"YouTube API ({path}): {str(last)[:200]}") from last


def _iso8601_seconds(s: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def resolve_channel(handle_or_id: str) -> dict[str, Any]:
    value = (handle_or_id or "").strip()
    if not value:
        if not _oauth():
            raise RuntimeError("Set YOUTUBE_CHANNEL (your @handle) or add client_secrets.json for OAuth.")
        data = _get("channels", part="snippet,contentDetails,statistics", mine=True)
    elif value.startswith("UC") and len(value) >= 20:
        data = _get("channels", part="snippet,contentDetails,statistics", id=value)
    else:
        data = _get("channels", part="snippet,contentDetails,statistics", forHandle=value.lstrip("@"))
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"channel {value or 'mine'!r} not found")
    ch = items[0]
    return {"id": ch["id"], "title": ch["snippet"]["title"],
            "uploads_playlist": ch["contentDetails"]["relatedPlaylists"]["uploads"],
            "subscribers": int(ch["statistics"].get("subscriberCount", 0)),
            "video_count": int(ch["statistics"].get("videoCount", 0))}


def fetch_uploads(max_videos: int = 200) -> dict[str, Any]:
    """Recent uploads with statistics, Shorts only (<= 180 s). Saved to data/channel_stats.json."""
    if not configured():
        raise RuntimeError("Add client_secrets.json (OAuth) or YOUTUBE_API_KEY + YOUTUBE_CHANNEL in Settings first.")
    channel = resolve_channel(env("YOUTUBE_CHANNEL") or "")
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
                "id": v["id"], "title": v["snippet"]["title"], "description": (v["snippet"].get("description") or "")[:1000],
                "published": v["snippet"]["publishedAt"], "published_ts": published,
                "age_hours": round(hours, 1), "duration": seconds, "views": views,
                "likes": int(st.get("likeCount", 0)), "comments": int(st.get("commentCount", 0)),
                "views_per_hour": round(views / hours, 2),
            })
    data = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "channel": channel, "videos": videos}
    save_json(STATS_PATH, data)
    log.info("channel %s: %d Shorts fetched", channel["title"], len(videos))
    return data


def _norm(text: str) -> str:
    text = re.sub(r"#\w+", " ", (text or "").lower())            # hashtags are compared separately
    return re.sub(r"[^a-z0-9 ]+", " ", text).split() and " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split()) or ""


def _tags(*texts: str) -> set[str]:
    return {t.lower() for text in texts for t in re.findall(r"#(\w+)", text or "")}


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0 if min(len(a), len(b)) >= 25 else 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def _render_ts(dir_name: str) -> float:
    try:  # output dirs are named <UTC timestamp>_<...>
        return datetime.strptime(dir_name[:20], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _match_score(meta: dict[str, Any], video: dict[str, Any], dir_name: str) -> float:
    """How likely `video` is the upload of this produced Short. Uploads happen after the render, and
    people paste our title, description or hashtags into any of YouTube's fields, so compare them all."""
    if video.get("published_ts") and video["published_ts"] < _render_ts(dir_name) - 60:
        return 0.0
    our_title = _norm(meta.get("title", ""))
    our_desc = _norm(meta.get("description", ""))
    our_desc_first = _norm((meta.get("description") or "").split("\n")[0])
    their_title = _norm(video.get("title", ""))
    their_desc = _norm(video.get("description", ""))
    text = max(_similar(our_title, their_title), _similar(our_title, their_desc),
               _similar(our_desc_first, their_title), _similar(our_desc, their_desc) if len(our_desc) > 30 else 0.0)
    ours = {t.lower() for t in meta.get("hashtags") or []} | _tags(meta.get("description", ""))
    theirs = _tags(video.get("title", ""), video.get("description", ""))
    tag_overlap = len(ours & theirs) / len(ours) if ours else 0.0
    duration_ok = abs(float(video.get("duration") or 0) - float(meta.get("duration") or -99)) <= 1.5
    score = text
    if tag_overlap >= 0.8 and len(ours) >= 3:
        score = max(score, 0.7 + (0.2 if duration_ok else 0.0))
    if duration_ok and text >= 0.5:
        score = max(score, text + 0.15)
    return min(score, 1.0)


def match_outputs(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach channel stats to produced Shorts: by our upload id, else by the best text / hashtag /
    duration match (each channel video is used at most once)."""
    by_id = {v["id"]: v for v in videos}
    matched: list[dict[str, Any]] = []
    if not OUTPUT_DIR.exists():
        return matched
    metas = []
    for d in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        meta = load_json(d / "meta.json")
        if meta:
            metas.append((d, meta))
    taken: set[str] = set()
    assignments: dict[str, dict[str, Any]] = {}
    for d, meta in metas:                                   # exact upload records first
        video = by_id.get(meta.get("youtube_id") or "")
        if video:
            assignments[d.name] = video
            taken.add(video["id"])
    pairs = []
    for d, meta in metas:
        if d.name in assignments:
            continue
        for v in videos:
            if v["id"] in taken:
                continue
            s = _match_score(meta, v, d.name)
            if s >= 0.75:
                pairs.append((s, d.name, v))
    for s, name, v in sorted(pairs, key=lambda x: -x[0]):    # best pairs claim their video first
        if name in assignments or v["id"] in taken:
            continue
        assignments[name] = v
        taken.add(v["id"])
    # duration + timing fallback for uploads with no usable text (e.g. titled with the date)
    for d, meta in metas:
        if d.name in assignments or not meta.get("duration"):
            continue
        cands = [v for v in videos if v["id"] not in taken and abs(v["duration"] - meta["duration"]) <= 1.5
                 and v.get("published_ts", 0) >= _render_ts(d.name) - 60]
        if len(cands) == 1:
            assignments[d.name] = cands[0]
            taken.add(cands[0]["id"])
    for d, meta in metas:
        video = assignments.get(d.name)
        if not video:
            continue
        bp = meta.get("blueprint") or {}
        changed = meta.get("youtube_id") != video["id"] or meta.get("channel_stats", {}).get("views") != video["views"]
        meta["youtube_id"] = video["id"]
        meta["channel_stats"] = {k: video[k] for k in ("views", "likes", "comments", "views_per_hour", "age_hours", "published")}
        meta["channel_stats"]["youtube_title"] = video["title"]
        if changed:
            save_json(d / "meta.json", meta)
        matched.append({"dir": d.name, "title": meta.get("title"), "key": f"{bp.get('format')}|{bp.get('topic')}",
                        "format": bp.get("format"), "topic": bp.get("topic"), **meta["channel_stats"]})
    return matched


def _shrunk_factor(vph: float, channel_median: float, n: int) -> float:
    """views/hour ratio to the channel median, pulled toward 1.0 when few videos support it:
    1 video -> half-way, 2 -> two thirds, 3 -> three quarters..."""
    if channel_median <= 0 or n <= 0:
        return 1.0
    raw = max(FACTOR_MIN, min(FACTOR_MAX, vph / channel_median))
    weight = n / (n + 1.0)
    return 1.0 + (raw - 1.0) * weight


def _group_stats(items: list[dict[str, Any]], channel_median: float) -> dict[str, Any]:
    vph = median(i["views_per_hour"] for i in items)
    return {"videos": len(items), "median_views_per_hour": round(vph, 2), "median_views": int(median(i["views"] for i in items)),
            "factor": round(_shrunk_factor(vph, channel_median, len(items)), 2), "provisional": len(items) < 3}


def blueprint_performance(matched: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """How each format x topic, each format and each topic performs on YOUR channel: median views/hour
    relative to the channel median, as a factor in [FACTOR_MIN, FACTOR_MAX] shrunk by sample size."""
    from .judge import canonical_topic

    if matched is None:
        data = load_json(STATS_PATH)
        matched = match_outputs(data["videos"]) if data else []
    mature = [m for m in matched if m.get("age_hours", 0) >= MIN_AGE_HOURS]
    if not mature:
        return {"channel_median_vph": None, "videos": len(matched), "blueprints": {}, "formats": {}, "topics": {}}
    channel_median = median(m["views_per_hour"] for m in mature) or 0.0
    pairs: dict[str, list[dict[str, Any]]] = {}
    formats: dict[str, list[dict[str, Any]]] = {}
    topics: dict[str, list[dict[str, Any]]] = {}
    for m in mature:
        fmt, topic = (m.get("format") or "custom"), canonical_topic(m.get("topic") or "custom")
        pairs.setdefault(f"{fmt}|{topic}", []).append(m)
        formats.setdefault(fmt, []).append(m)
        topics.setdefault(topic, []).append(m)
    return {"channel_median_vph": round(channel_median, 2), "videos": len(mature),
            "blueprints": {k: _group_stats(v, channel_median) for k, v in pairs.items()},
            "formats": {k: _group_stats(v, channel_median) for k, v in formats.items()},
            "topics": {k: _group_stats(v, channel_median) for k, v in topics.items()}}


def factor_for(perf: dict[str, Any], fmt: str, topic: str) -> tuple[float, int, str]:
    """Factor for a blueprint: the exact pair when seen, else the format and topic factors combined
    (geometric mean), else neutral. Returns (factor, videos behind it, basis)."""
    from .judge import canonical_topic

    topic = canonical_topic(topic)
    pair = (perf.get("blueprints") or {}).get(f"{fmt}|{topic}")
    if pair:
        return float(pair["factor"]), int(pair["videos"]), "pair"
    f = (perf.get("formats") or {}).get(fmt)
    t = (perf.get("topics") or {}).get(topic)
    if f and t:
        return round((float(f["factor"]) * float(t["factor"])) ** 0.5, 2), min(int(f["videos"]), int(t["videos"])), "format+topic"
    if f:
        return float(f["factor"]), int(f["videos"]), "format"
    if t:
        return float(t["factor"]), int(t["videos"]), "topic"
    return 1.0, 0, "none"


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
