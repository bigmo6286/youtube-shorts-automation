"""Retention from the YouTube Analytics API (free, read-only, same Google login).

Views per hour says how much YouTube pushed a Short; retention says why. For every video on the channel this reads
average percentage viewed (over 100% means people re-watched the loop), average view duration and engaged views,
for the last 90 days, into data/channel_analytics.json. `channel.blueprint_performance` blends it into each format x
topic factor, and the script writer gets the channel's best-retaining openings as examples of technique.

Needs the yt-analytics.readonly permission (Connect YouTube Analytics in the console, or `channel connect-analytics`)
and the "YouTube Analytics API" enabled in the Google Cloud project. Analytics lag about two days behind.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from .config import DATA_DIR
from .storage import load_json, save_json

log = logging.getLogger(__name__)

ANALYTICS_PATH = DATA_DIR / "channel_analytics.json"
DAYS = 90
MIN_VIEWS = 20                   # below this, average % viewed is noise
ENABLE_URL = "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com"
BASE_METRICS = ["views", "averageViewDuration", "averageViewPercentage", "likes", "subscribersGained"]


def connected() -> bool:
    from .upload import ANALYTICS_SCOPE, has_scope
    return has_scope(ANALYTICS_SCOPE)


def _service(interactive: bool = False):
    from googleapiclient.discovery import build
    from .upload import ANALYTICS_SCOPE, _credentials, with_retries
    creds = with_retries(lambda: _credentials(interactive=interactive, extra_scopes=(ANALYTICS_SCOPE,)),
                         "connect to YouTube Analytics")
    return build("youtubeAnalytics", "v2", credentials=creds, static_discovery=True)


def _explain(exc: Exception) -> RuntimeError:
    text = str(exc)
    if "accessNotConfigured" in text or "has not been used" in text or "is disabled" in text:
        return RuntimeError(f"The YouTube Analytics API is not enabled in your Google Cloud project. Enable it at "
                            f"{ENABLE_URL} (same project as your client_secrets.json), wait a minute, then sync again.")
    if "insufficient" in text.lower() and "scope" in text.lower():
        return RuntimeError("Google did not grant YouTube Analytics access; use Connect YouTube Analytics again and "
                            "approve every permission.")
    return RuntimeError(f"YouTube Analytics: {text[:300]}")


def fetch(interactive: bool = False) -> dict[str, Any]:
    """Per-video retention for the last DAYS days; saved and returned."""
    from .upload import with_retries
    yt = _service(interactive)
    end = date.today()
    start = end - timedelta(days=DAYS)
    rows: list[list[Any]] = []
    headers: list[str] = []
    for metrics in (BASE_METRICS + ["engagedViews"], BASE_METRICS):     # engagedViews is newer; not every account has it
        try:
            resp = with_retries(lambda: yt.reports().query(
                ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(), metrics=",".join(metrics),
                dimensions="video", sort="-views", maxResults=200).execute(), "read YouTube Analytics")
        except Exception as exc:  # noqa: BLE001
            if "engagedViews" in metrics and "engagedViews" in str(exc):
                continue
            raise _explain(exc) from exc
        headers = [h["name"] for h in resp.get("columnHeaders", [])]
        rows = resp.get("rows") or []
        break
    videos: dict[str, dict[str, Any]] = {}
    for row in rows:
        r = dict(zip(headers, row))
        vid = r.pop("video")
        views = int(r.get("views") or 0)
        entry = {"views": views, "avg_view_pct": round(float(r.get("averageViewPercentage") or 0), 1),
                 "avg_view_seconds": round(float(r.get("averageViewDuration") or 0), 1),
                 "likes": int(r.get("likes") or 0), "subscribers_gained": int(r.get("subscribersGained") or 0)}
        if "engagedViews" in r and views:
            entry["engaged_views"] = int(r["engagedViews"] or 0)
            entry["engaged_ratio"] = round(entry["engaged_views"] / views, 3)
        videos[vid] = entry
    data = {"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"), "start": start.isoformat(),
            "end": end.isoformat(), "videos": videos}
    save_json(ANALYTICS_PATH, data)
    log.info("YouTube Analytics: retention for %d videos (%s to %s)", len(videos), start, end)
    return data


def cached() -> dict[str, Any]:
    return load_json(ANALYTICS_PATH) or {"videos": {}}


def retention_for(video_id: str) -> dict[str, Any] | None:
    v = cached()["videos"].get(video_id)
    return v if v and v.get("views", 0) >= MIN_VIEWS else None


def sync_quietly() -> str:
    """Refresh analytics during a channel sync without ever opening a browser. Returns a status line."""
    if not connected():
        return "YouTube Analytics not connected (use Connect YouTube Analytics in the console for retention data)"
    try:
        data = fetch(interactive=False)
        usable = sum(1 for v in data["videos"].values() if v.get("views", 0) >= MIN_VIEWS)
        return f"YouTube Analytics: retention for {len(data['videos'])} videos ({usable} with {MIN_VIEWS}+ views)"
    except Exception as exc:  # noqa: BLE001 - views/hour feedback still works without retention
        log.warning("%s", exc)
        return str(exc)
