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

from .config import DATA_DIR, OUTPUT_DIR, env, load_config
from .storage import JsonCache, load_json, save_json

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
STATS_PATH = DATA_DIR / "channel_stats.json"
MIN_VIDEOS_PER_BLUEPRINT = 1   # one video already counts, shrunk toward neutral until more arrive
FACTOR_MIN, FACTOR_MAX = 0.2, 4.0
MIN_AGE_HOURS = 3              # a video younger than this has no meaningful views-per-hour yet
RATE_WINDOW_HOURS = 14 * 24    # views/hour is measured over at most the first two weeks (Shorts peak early)
MAX_CHANNEL_WINNERS = 10
RETENTION_WEIGHT = 0.4         # share of the factor that comes from retention when YouTube Analytics is connected
RETENTION_MIN, RETENTION_MAX = 0.5, 2.0


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
        batch = _get("videos", part="snippet,statistics,contentDetails,status", id=",".join(ids[i:i + 50]))
        for v in batch.get("items", []):
            seconds = _iso8601_seconds(v["contentDetails"].get("duration", ""))
            if seconds == 0 or seconds > 180:
                continue
            published = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")).timestamp()
            hours = max(1.0, (now - published) / 3600.0)
            st = v.get("statistics", {})
            views = int(st.get("viewCount", 0))
            privacy = (v.get("status") or {}).get("privacyStatus", "public")
            # views per hour over the first RATE_WINDOW_HOURS only: a Short gets most of its views early, so an old
            # video is not punished for having stopped growing, and a 3-day-old one is comparable to a 3-month-old one
            public_hours = hours if privacy == "public" else 0.0
            videos.append({
                "id": v["id"], "title": v["snippet"]["title"], "description": (v["snippet"].get("description") or "")[:1000],
                "published": v["snippet"]["publishedAt"], "published_ts": published,
                "age_hours": round(hours, 1), "duration": seconds, "views": views, "privacy": privacy,
                "likes": int(st.get("likeCount", 0)), "comments": int(st.get("commentCount", 0)),
                "views_per_hour": round(views / min(hours, RATE_WINDOW_HOURS), 2), "public_hours": round(public_hours, 1),
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
        changed = (meta.get("youtube_id") != video["id"] or meta.get("channel_stats", {}).get("views") != video["views"]
                   or (video.get("privacy") and meta.get("privacy") != video["privacy"]))
        meta["youtube_id"] = video["id"]
        if video.get("privacy"):
            meta["privacy"] = video["privacy"]           # follows changes made in YouTube Studio
        meta["channel_stats"] = {k: video[k] for k in ("views", "likes", "comments", "views_per_hour", "age_hours", "published")}
        meta["channel_stats"]["youtube_title"] = video["title"]
        if changed:
            save_json(d / "meta.json", meta)
        matched.append({"dir": d.name, "id": video["id"], "title": meta.get("title"), "key": f"{bp.get('format')}|{bp.get('topic')}",
                        "format": bp.get("format"), "topic": bp.get("topic"), "hook_style": bp.get("hook_style"),
                        "transcript": (load_json(d / "script.json") or {}).get("full_text", "")[:500], **meta["channel_stats"]})
    return matched


_LABELS = JsonCache("channel_judgments")


def label_uploads(videos: list[dict[str, Any]], *, max_new: int = 120) -> int:
    """Give every upload a format / topic / hook label by judging it like a trending Short (yt-dlp metadata
    + transcript, TypeSafe). Labels are cached per video and taxonomy version, so this costs something only
    for new uploads. Makes the feedback independent of which machine produced the video or whether its
    output folder still exists."""
    from . import fetch, judge

    if not judge.has_typesafe():
        return 0
    fetch.configure(load_config().get("discovery") or {})
    scripts = _local_scripts()
    done = 0
    for v in videos:
        key = f"{v['id']}+{judge.TAXONOMY_VERSION}"
        cached = _LABELS.get(key)
        if cached:
            v.update(cached)
            continue
        if done >= max_new:
            continue
        meta = None if fetch.rate_limited else fetch.fetch_full(v["id"], True, 700)
        if not meta:
            # YouTube blocks yt-dlp now and then ("confirm you're not a bot"). For our own uploads that lookup is not
            # needed: the API gave us the title and description, and the script we wrote is the transcript.
            meta = {"id": v["id"], "title": v.get("title", ""), "description": v.get("description", ""),
                    "duration": v.get("duration"), "transcript": scripts.get(v["id"], ""), "tags": [], "categories": []}
            if not meta["transcript"] and not meta["description"]:
                continue
        try:
            j = judge.judge_short(meta)
        except Exception as exc:  # noqa: BLE001
            log.warning("labelling %s failed: %s", v["id"], str(exc)[:120])
            continue
        if not j:
            continue
        labels = {"format": j["format"]["choice"], "topic": j["topic"]["choice"], "hook_style": j["hook_style"]["choice"],
                  "format_confidence": j["format"]["confidence"], "transcript": (meta.get("transcript") or "")[:500]}
        _LABELS.set(key, labels)
        v.update(labels)
        done += 1
    return done


def _local_scripts() -> dict[str, str]:
    """youtube_id -> spoken script text, for Shorts produced on this machine."""
    out: dict[str, str] = {}
    if not OUTPUT_DIR.exists():
        return out
    for d in OUTPUT_DIR.iterdir():
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if meta and meta.get("youtube_id"):
            text = (load_json(d / "script.json") or {}).get("full_text") or ""
            if text:
                out[meta["youtube_id"]] = text[:700]
    return out


def _shrunk_factor(vph: float, channel_median: float, n: int) -> float:
    """views/hour ratio to the channel median, pulled toward 1.0 when few videos support it:
    1 video -> half-way, 2 -> two thirds, 3 -> three quarters..."""
    if channel_median <= 0 or n <= 0:
        return 1.0
    raw = max(FACTOR_MIN, min(FACTOR_MAX, vph / channel_median))
    weight = n / (n + 1.0)
    return 1.0 + (raw - 1.0) * weight


def _retention_factor(pct: float, channel_pct: float, n: int) -> float:
    """Average % viewed relative to the channel median, bounded and shrunk toward 1.0 like the views factor."""
    if channel_pct <= 0 or n <= 0:
        return 1.0
    raw = max(RETENTION_MIN, min(RETENTION_MAX, pct / channel_pct))
    return 1.0 + (raw - 1.0) * (n / (n + 1.0))


def _group_stats(items: list[dict[str, Any]], channel_median: float, channel_pct: float | None = None) -> dict[str, Any]:
    vph = median(i["views_per_hour"] for i in items)
    vph_factor = _shrunk_factor(vph, channel_median, len(items))
    out = {"videos": len(items), "median_views_per_hour": round(vph, 2), "median_views": int(median(i["views"] for i in items)),
           "factor": round(vph_factor, 2), "views_factor": round(vph_factor, 2), "provisional": len(items) < 3}
    kept = [float(i["avg_view_pct"]) for i in items if i.get("avg_view_pct") is not None]
    if kept and channel_pct:
        pct = median(kept)
        rf = _retention_factor(pct, channel_pct, len(kept))
        # Views/hour says how far YouTube pushed it, retention says whether viewers stayed: blend both.
        out.update(median_view_pct=round(pct, 1), retention_videos=len(kept), retention_factor=round(rf, 2),
                   factor=round(vph_factor ** (1 - RETENTION_WEIGHT) * rf ** RETENTION_WEIGHT, 2))
    return out


def labelled_uploads() -> list[dict[str, Any]]:
    """Every upload on the channel that carries a label (from the cache; `sync` adds new ones)."""
    from . import judge

    from .analytics import retention_for

    data = load_json(STATS_PATH)
    if not data:
        return []
    out = []
    for v in data["videos"]:
        cached = _LABELS.get(f"{v['id']}+{judge.TAXONOMY_VERSION}")
        if cached:
            ret = retention_for(v["id"]) or {}
            out.append({**v, **cached, **{k: ret[k] for k in ("avg_view_pct", "avg_view_seconds", "engaged_ratio") if k in ret}})
    return out


def blueprint_performance(matched: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """How each format x topic, each format and each topic performs on YOUR channel: median views/hour
    relative to the channel median, as a factor in [FACTOR_MIN, FACTOR_MAX] shrunk by sample size.
    Uses every labelled upload on the channel (both machines, deleted outputs included); locally
    produced videos without a label fall back to their own blueprint labels."""
    from .judge import canonical_topic

    uploads = labelled_uploads()
    seen = {u["id"] for u in uploads}
    if matched is None:
        data = load_json(STATS_PATH)
        matched = match_outputs(data["videos"]) if data else []
    for m in matched:
        vid = None
        for d_meta in (m,):
            vid = d_meta.get("id")
        if vid and vid in seen:
            continue
        uploads.append(dict(m))
    # Private / unlisted uploads (the review queue) have no audience, so they do not count as failures.
    mature = [m for m in uploads if m.get("age_hours", 0) >= MIN_AGE_HOURS and m.get("format")
              and m.get("privacy", "public") == "public"]
    if not mature:
        return {"channel_median_vph": None, "videos": len(uploads), "blueprints": {}, "formats": {}, "topics": {}}
    channel_median = median(m["views_per_hour"] for m in mature) or 0.0
    with_ret = [float(m["avg_view_pct"]) for m in mature if m.get("avg_view_pct") is not None]
    channel_pct = median(with_ret) if with_ret else None
    pairs: dict[str, list[dict[str, Any]]] = {}
    formats: dict[str, list[dict[str, Any]]] = {}
    topics: dict[str, list[dict[str, Any]]] = {}
    hooks: dict[str, list[dict[str, Any]]] = {}
    for m in mature:
        fmt, topic = (m.get("format") or "custom"), canonical_topic(m.get("topic") or "custom")
        pairs.setdefault(f"{fmt}|{topic}", []).append(m)
        formats.setdefault(fmt, []).append(m)
        topics.setdefault(topic, []).append(m)
        if m.get("hook_style"):
            hooks.setdefault(m["hook_style"], []).append(m)
    stats = lambda groups: {k: _group_stats(v, channel_median, channel_pct) for k, v in groups.items()}  # noqa: E731
    return {"channel_median_vph": round(channel_median, 2), "videos": len(mature), "uploads_labelled": len(uploads),
            "channel_median_view_pct": round(channel_pct, 1) if channel_pct else None, "retention_videos": len(with_ret),
            "blueprints": stats(pairs), "formats": stats(formats), "topics": stats(topics), "hooks": stats(hooks)}


def best_openings(n: int = 3) -> list[dict[str, Any]]:
    """The channel's best-retaining openings, for the script writer. For Shorts made on this machine the exact hook
    from the script is used; otherwise the first sentence of the captions, cleaned of [music] and >> markers."""
    import re
    hooks = _local_hooks()
    by_title = _history_hooks()
    rows = []
    for u in labelled_uploads():
        if u.get("avg_view_pct") is None:
            continue
        hooks.setdefault(u["id"], by_title.get(_norm(u.get("title", "")), ""))
        if hooks[u["id"]] or (u.get("transcript") or "").strip():
            rows.append(u)
    rows.sort(key=lambda u: -float(u["avg_view_pct"]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in rows:
        first = hooks.get(u["id"])
        if not first:
            text = re.sub(r"\[[^\]]*\]|>>", " ", u["transcript"])
            text = re.sub(r"([.!?])(?=[A-Z])", r"\1 ", text)            # "men.When" -> "men. When"
            text = re.sub(r"\s+", " ", text).strip()
            first = re.split(r"(?<=[.!?])\s+", text)[0]
            if re.search(r"[a-z]{3,}[a-z]{9,}", first):                 # words glued by old caption joining: skip
                continue
        first = first[:180]
        key = re.sub(r"\W+", "", first.lower())[:60]
        if len(first.split()) >= 4 and key not in seen:
            seen.add(key)
            out.append({"opening": first, "avg_view_pct": u["avg_view_pct"], "hook_style": u.get("hook_style"),
                        "title": u.get("title", "")})
        if len(out) >= n:
            break
    return out


def _history_hooks() -> dict[str, str]:
    """normalised title -> opening sentence, from the permanent produce history (survives deleted outputs)."""
    import re
    out: dict[str, str] = {}
    for v in (load_json(DATA_DIR / "produced_titles.json") or {}).get("videos", []):
        hook = (v.get("hook") or "").strip()
        if v.get("title") and hook:
            out[_norm(v["title"])] = re.split(r"(?<=[.!?])\s+", hook)[0]
    return out


def _local_hooks() -> dict[str, str]:
    """youtube_id -> the opening line we wrote, for Shorts produced on this machine."""
    out: dict[str, str] = {}
    if not OUTPUT_DIR.exists():
        return out
    for d in OUTPUT_DIR.iterdir():
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if meta and meta.get("youtube_id"):
            hook = (load_json(d / "script.json") or {}).get("hook")
            if hook:
                out[meta["youtube_id"]] = hook
    return out


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


CHANNEL_BLUEPRINTS_PATH = DATA_DIR / "channel_blueprints.json"


def channel_blueprints(perf: dict[str, Any] | None = None, min_factor: float = 1.3) -> list[dict[str, Any]]:
    """Blueprints built from what already performs on YOUR channel, independent of the trend run.
    One per format x topic whose factor beats `min_factor`, modelled on your best uploads of that pair."""
    from .judge import canonical_topic

    perf = perf or blueprint_performance()
    pairs = perf.get("blueprints") or {}
    if not pairs:
        return []
    uploads = labelled_uploads()
    data = load_json(STATS_PATH)
    matched = match_outputs(data["videos"]) if data else []
    seen = {u["id"] for u in uploads}
    uploads += [m for m in matched if m.get("id") not in seen]
    by_key: dict[str, list[dict[str, Any]]] = {}
    for u in uploads:
        if not u.get("format") or u.get("format") in ("custom", "other", "uncertain") or u.get("privacy", "public") != "public":
            continue
        by_key.setdefault(f"{u['format']}|{canonical_topic(u.get('topic', ''))}", []).append(u)
    out = []
    for key, p in pairs.items():
        if float(p["factor"]) < min_factor or key not in by_key or key.endswith("|other"):
            continue
        items = sorted(by_key[key], key=lambda u: -float(u.get("views_per_hour") or 0))
        best = items[0]
        hooks = [u.get("hook_style") for u in items if u.get("hook_style") not in (None, "no_hook", "visual_only")]
        hook = max(set(hooks), key=hooks.count) if hooks else "curiosity_gap"
        fmt, topic = key.split("|", 1)
        out.append({
            "key": key, "format": fmt, "topic": topic, "hook_style": hook,
            "source": "channel", "opportunity": round(float(p["factor"]), 3), "count": int(p["videos"]), "stretch": False,
            "median_replicable": 1.0, "median_hook": 0.0, "median_views_per_hour": p["median_views_per_hour"],
            "median_duration": best.get("duration", 40),
            "why_it_works": (f"this format x topic performs x{p['factor']} your channel median ({p['videos']} video"
                             f"{'s' if p['videos'] != 1 else ''}, {p['median_views_per_hour']} views/hour); your best one: "
                             f"{best.get('title', '')!r} ({int(best.get('views') or 0):,} views)"),
            "exemplars": [{"id": u["id"], "title": u.get("title", ""), "url": f"https://youtube.com/shorts/{u['id']}",
                           "views": int(u.get("views") or 0), "views_per_hour": float(u.get("views_per_hour") or 0),
                           "transcript": (u.get("transcript") or "")[:400]} for u in items[:3]],
        })
    out.sort(key=lambda b: (-b["opportunity"], -b["count"]))
    out = out[:MAX_CHANNEL_WINNERS]
    save_json(CHANNEL_BLUEPRINTS_PATH, out)
    return out


def sync() -> dict[str, Any]:
    data = fetch_uploads()
    new = label_uploads(data["videos"])
    if new:
        log.info("channel feedback: labelled %d new upload(s) with TypeSafe", new)
    from .analytics import sync_quietly
    log.info("%s", sync_quietly())
    matched = match_outputs(data["videos"])
    perf = blueprint_performance(matched)
    log.info("channel feedback: %d uploads labelled, %d matched to local outputs, %d format x topic pairs with data",
             perf.get("uploads_labelled", 0), len(matched), len(perf["blueprints"]))
    if perf.get("channel_median_view_pct"):
        log.info("retention: channel median %.0f%% viewed over %d videos; factors blend views/hour and retention",
                 perf["channel_median_view_pct"], perf["retention_videos"])
    try:
        from . import tuning
        tuning.update()                                   # daily upload volume and script length from these numbers
    except Exception as exc:  # noqa: BLE001
        log.warning("tuning skipped: %s", exc)
    return {"channel": data["channel"], "fetched_at": data["fetched_at"], "uploads": len(data["videos"]),
            "matched": matched, "performance": perf}


def cached_report() -> dict[str, Any] | None:
    data = load_json(STATS_PATH)
    if not data:
        return None
    matched = match_outputs(data["videos"])
    perf = blueprint_performance(matched)
    from . import analytics
    an = analytics.cached()
    return {"channel": data["channel"], "fetched_at": data["fetched_at"], "uploads": len(data["videos"]),
            "matched": matched, "performance": perf, "winners": load_json(CHANNEL_BLUEPRINTS_PATH) or [],
            "analytics": {"connected": analytics.connected(), "fetched_at": an.get("fetched_at"),
                          "videos": len(an.get("videos") or {})}}
