"""Discover candidate Shorts with yt-dlp and pull full metadata + transcripts.

No YouTube API key needed. Sources:
  * hashtag Shorts shelves  https://www.youtube.com/hashtag/<tag>/shorts
  * search pages filtered to "this week" + "short" duration
  * optional: YouTube Data API `mostPopular` chart when YOUTUBE_API_KEY is set
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests
import yt_dlp

from .config import env
from .storage import JsonCache

log = logging.getLogger(__name__)

# YouTube search "sp" filter: upload date = this week, duration = short (<4 min), sort = view count
SEARCH_SP_THIS_WEEK_SHORT_BY_VIEWS = "CAMSBAgDGAE%3D"
SEARCH_SP_THIS_WEEK_SHORT = "EgQIAxgB"

FLAT_OPTS = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
# Gentle defaults: YouTube starts answering "sign in to confirm you're not a bot" after a few hundred
# fast anonymous fetches. Spacing requests out avoids most of that; cookies avoid the rest.
FULL_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True,
             "extractor_retries": 2, "sleep_interval_requests": 0.7}


def configure(discovery_cfg: dict[str, Any]) -> None:
    """Apply config.yaml discovery settings that affect yt-dlp (call once from the CLI)."""
    browser = discovery_cfg.get("cookies_from_browser")
    for opts in (FLAT_OPTS, FULL_OPTS):
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
        else:
            opts.pop("cookiesfrombrowser", None)
    FULL_OPTS["sleep_interval_requests"] = float(discovery_cfg.get("request_spacing_seconds", 0.7))


def _flat_entries(url: str, limit: int) -> list[dict[str, Any]]:
    opts = dict(FLAT_OPTS, playlistend=limit)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:  # noqa: BLE001 - one bad source must not kill discovery
        log.warning("discovery source failed %s: %s", url, exc)
        return []
    entries = info.get("entries") or []
    out = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        if e.get("live_status") in ("is_live", "is_upcoming", "was_live"):
            continue
        out.append({"id": e["id"], "title": e.get("title"), "view_count": e.get("view_count"),
                    "duration": e.get("duration"), "source": url})
    return out


def discover(hashtags: Iterable[str], queries: Iterable[str], per_source_limit: int) -> list[dict[str, Any]]:
    """Return de-duplicated flat candidates from every configured source."""
    sources = [f"https://www.youtube.com/hashtag/{quote_plus(tag.lstrip('#'))}/shorts" for tag in hashtags]
    for q in queries:
        sources.append(f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp={SEARCH_SP_THIS_WEEK_SHORT_BY_VIEWS}")
        sources.append(f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp={SEARCH_SP_THIS_WEEK_SHORT}")

    seen: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_flat_entries, url, per_source_limit): url for url in sources}
        for fut in as_completed(futures):
            for e in fut.result():
                if e["id"] not in seen:
                    seen[e["id"]] = e
    log.info("discovered %d unique candidates from %d sources", len(seen), len(sources))
    return list(seen.values())


def discover_channel_shorts(channel_ids: Iterable[str], per_channel: int) -> list[dict[str, Any]]:
    """Newest Shorts from channels that already have a viral Short: the freshest signal of what is working."""
    urls = [f"https://www.youtube.com/channel/{cid}/shorts" for cid in dict.fromkeys(c for c in channel_ids if c)]
    seen: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for entries in pool.map(lambda u: _flat_entries(u, per_channel), urls):
            for e in entries:
                seen.setdefault(e["id"], e)
    log.info("channel Shorts tabs: %d recent candidates from %d channels", len(seen), len(urls))
    return list(seen.values())


def discover_api_most_popular(region: str = "US", limit: int = 50) -> list[dict[str, Any]]:
    """Optional extra source using the official API (needs YOUTUBE_API_KEY)."""
    key = env("YOUTUBE_API_KEY")
    if not key:
        return []
    url = ("https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails,statistics"
           f"&chart=mostPopular&regionCode={region}&maxResults={min(limit, 50)}&key={key}")
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("YouTube API mostPopular failed: %s", exc)
        return []
    out = []
    for item in r.json().get("items", []):
        out.append({"id": item["id"], "title": item["snippet"]["title"],
                    "view_count": int(item["statistics"].get("viewCount", 0)),
                    "duration": _iso8601_seconds(item["contentDetails"].get("duration", "")),
                    "source": "api:mostPopular"})
    return out


def _iso8601_seconds(s: str) -> int | None:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


# --------------------------------------------------------------------------- full metadata

_META_CACHE = JsonCache("video_meta")

# Circuit breaker: once YouTube starts answering 429 / "confirm you're not a bot", every further
# request only digs the hole deeper. After this many consecutive failures the stage gives up.
RATE_LIMIT_TRIP = 8
_rate_limit_lock = threading.Lock()
_consecutive_failures = 0
rate_limited = False


def _note_result(ok: bool, message: str = "") -> None:
    global _consecutive_failures, rate_limited
    with _rate_limit_lock:
        if ok:
            _consecutive_failures = 0
            return
        _consecutive_failures += 1
        if _consecutive_failures >= RATE_LIMIT_TRIP and not rate_limited:
            rate_limited = True
            log.error("YouTube is rate limiting this machine (%d fetches failed in a row: %s). Stopping metadata "
                      "fetches early; wait an hour, lower discovery.workers, or set cookies_from_browser.",
                      _consecutive_failures, message[:80])


def fetch_full(video_id: str, want_transcript: bool, transcript_chars: int, *, force: bool = False) -> dict[str, Any] | None:
    cached = _META_CACHE.get(video_id)
    if cached and not force and (cached.get("transcript") is not None or not want_transcript):
        return cached
    if rate_limited:
        return cached
    url = f"https://www.youtube.com/shorts/{video_id}"
    try:
        with yt_dlp.YoutubeDL(FULL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        _note_result(False, msg)
        log.warning("metadata failed for %s: %s", video_id, msg.splitlines()[0][:160])
        return cached
    _note_result(True)

    meta = {
        "id": video_id,
        "url": url,
        "title": info.get("title") or "",
        "description": (info.get("description") or "")[:1000],
        "duration": info.get("duration"),
        "view_count": info.get("view_count") or 0,
        "like_count": info.get("like_count") or 0,
        "comment_count": info.get("comment_count") or 0,
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_id": info.get("channel_id"),
        "channel_followers": info.get("channel_follower_count") or 0,
        "timestamp": info.get("timestamp"),
        "upload_date": info.get("upload_date"),
        "categories": info.get("categories") or [],
        "tags": (info.get("tags") or [])[:20],
        "language": info.get("language"),
        "width": info.get("width"),
        "height": info.get("height"),
        "fetched_at": time.time(),
        "transcript": None,
    }
    if want_transcript:
        meta["transcript"] = _transcript(info, transcript_chars)
    _META_CACHE.set(video_id, meta)
    return meta


def _transcript(info: dict[str, Any], max_chars: int) -> str:
    caps = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    tracks = None
    for pool in (caps, auto):
        for lang in ("en", "en-orig", "en-US", "en-GB"):
            if pool.get(lang):
                tracks = pool[lang]
                break
        if tracks:
            break
    if not tracks:
        return ""
    fmt = next((t for t in tracks if t.get("ext") == "json3"), None) or tracks[0]
    try:
        r = requests.get(fmt["url"], timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception:  # noqa: BLE001
        return ""
    lines: list[str] = []
    for ev in data.get("events", []):
        # segments inside one caption event carry their own spacing; separate events (lines) need a space between
        line = "".join(seg.get("utf8", "") for seg in ev.get("segs") or [] if seg.get("utf8", "") != "\n")
        if line.strip():
            lines.append(line.strip())
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return text[:max_chars]


def enrich(candidates: list[dict[str, Any]], *, max_candidates: int, workers: int,
           want_transcript: bool, transcript_chars: int) -> list[dict[str, Any]]:
    """Full-metadata fetch for the most-viewed candidates first."""
    ordered = sorted(candidates, key=lambda c: c.get("view_count") or 0, reverse=True)[:max_candidates]
    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_full, c["id"], want_transcript, transcript_chars) for c in ordered]
        for fut in as_completed(futures):
            meta = fut.result()
            if meta:
                out.append(meta)
    return out


def is_short(meta: dict[str, Any], max_duration: int) -> bool:
    d = meta.get("duration")
    if d is None or d > max_duration:
        return False
    w, h = meta.get("width"), meta.get("height")
    if w and h and w > h:
        return False
    return True


_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def probably_non_english(meta: dict[str, Any]) -> bool:
    """Cheap code-side check so obviously non-Latin-script Shorts are neither judged nor ranked.
    The TypeSafe `english_ok` Noul still makes the final call for everything that passes."""
    text = f"{meta.get('title', '')} {(meta.get('description') or '')[:200]}"
    letters = _LETTER.findall(text)
    if len(letters) >= 8:
        latin = sum(1 for ch in letters if _LATIN.match(ch))
        if latin / len(letters) < 0.6:
            return True
    lang = (meta.get("language") or "").lower()
    return bool(lang) and not lang.startswith("en") and not meta.get("transcript")


def age_hours(meta: dict[str, Any]) -> float | None:
    ts = meta.get("timestamp")
    if not ts and meta.get("upload_date"):
        ts = datetime.strptime(meta["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    if not ts:
        return None
    return max(1.0, (time.time() - ts) / 3600.0)
