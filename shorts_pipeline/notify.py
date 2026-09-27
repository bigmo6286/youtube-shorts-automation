"""Telegram delivery: send a finished Short with its title, description and hashtags to a chat."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

from .config import env

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024       # Telegram caption limit for media
MESSAGE_LIMIT = 4096
VIDEO_LIMIT = 50 * 1024 * 1024   # bot API upload limit


def telegram_configured() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"


def _check(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"Telegram returned non-JSON: {resp.text[:200]}")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error {data.get('error_code')}: {data.get('description')}")
    return data["result"]


def discover_chats() -> list[dict[str, Any]]:
    """Chats that have messaged the bot recently (send the bot any message first, then call this)."""
    if not env("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    result = _check(requests.get(_api("getUpdates"), params={"limit": 100}, timeout=20))
    seen: dict[str, dict[str, Any]] = {}
    for upd in result:
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member", {}) or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is None:
            continue
        name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
        seen[str(chat["id"])] = {"id": str(chat["id"]), "type": chat.get("type"), "name": name}
    return list(seen.values())


def send_message(text: str) -> None:
    for i in range(0, max(len(text), 1), MESSAGE_LIMIT):
        _check(requests.post(_api("sendMessage"), data={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text[i:i + MESSAGE_LIMIT],
                                                       "disable_web_page_preview": True}, timeout=30))


def upload_text(meta: dict[str, Any]) -> str:
    """Title, description and hashtags exactly as they should be pasted into YouTube."""
    title = meta.get("title", "")
    desc = (meta.get("description") or "").strip()
    tags = ["#" + t.lstrip("#") for t in meta.get("hashtags") or []]
    missing = [t for t in tags if t.lower() not in desc.lower()]
    body = desc + ("\n\n" + " ".join(missing) if missing else "")
    return body if body.lower().startswith(title.lower()) else f"{title}\n\n{body}"


def send_short(video_path: Path, meta: dict[str, Any], *, note: str = "") -> None:
    """Send the video with its upload text as caption; long text follows as a separate message."""
    if not telegram_configured():
        raise RuntimeError("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Settings")
    text = upload_text(meta)
    if note:
        text = f"{note}\n\n{text}"
    caption = text if len(text) <= CAPTION_LIMIT else meta.get("title", "")[:CAPTION_LIMIT]
    size = video_path.stat().st_size
    if size > VIDEO_LIMIT:
        log.warning("video is %.1f MB, above Telegram's 50 MB bot limit; sending text only", size / 1e6)
        send_message(f"{text}\n\n(video too large for Telegram: {video_path})")
        return
    with open(video_path, "rb") as f:
        _check(requests.post(_api("sendVideo"),
                             data={"chat_id": env("TELEGRAM_CHAT_ID"), "caption": caption, "supports_streaming": "true",
                                   "width": 1080, "height": 1920},
                             files={"video": (video_path.name, f, "video/mp4")}, timeout=300))
    if caption != text:
        send_message(text)
    log.info("sent to Telegram chat %s", env("TELEGRAM_CHAT_ID"))
