"""Control the engine from Telegram.

The console polls the bot for messages and obeys only the configured TELEGRAM_CHAT_ID (everyone else is ignored).
Commands:
  /status            schedule, queue and what was learned
  /queue             Shorts waiting to upload, best first
  /videos            Shorts made but not on YouTube yet, numbered     /upload N   upload video N now
  /uploads           the latest uploads, numbered, with their privacy or publish time
  /publish N         make upload N public now          /private N   keep upload N private (cancels its publish time)
  /drafts            comment reply drafts, numbered    /reply N [text]   post draft N (optionally edited)
  /dismiss N         drop draft N                      /report      the daily report now
  /approve N, /reject N   decide a Short waiting for approval (numbers from /videos); the buttons under a video do the same
  /update            pull the latest engine code from GitHub and restart the console
  /pause, /resume    stop or restart scheduled production
  /stop              stop the job running right now
If two machines share one bot token, each sees only some messages: give each machine its own bot.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from .config import DATA_DIR, OUTPUT_DIR, env, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "telegram_bot.json"
_thread: threading.Thread | None = None


def enabled() -> bool:
    tcfg = (load_config().get("notifications") or {}).get("telegram") or {}
    return bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID") and tcfg.get("commands", True))


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"


def _say(text: str) -> None:
    from .notify import send_message
    send_message(text)


# ---------------------------------------------------------------------------------------------------- commands
def _uploads(n: int = 8) -> list[tuple[Any, dict[str, Any]]]:
    rows = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            meta = load_json(d / "meta.json") if d.is_dir() else None
            if meta and meta.get("youtube_id") and meta.get("privacy") != "deleted":
                rows.append((d, meta))
            if len(rows) >= n:
                break
    return rows


def _not_uploaded(n: int = 10) -> list[tuple[Any, dict[str, Any]]]:
    """Newest first: Shorts with a video file that are not on YouTube (queued, waiting for review, expired...)."""
    rows = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            meta = load_json(d / "meta.json") if d.is_dir() else None
            if meta and not meta.get("youtube_id") and (d / "short.mp4").exists():
                rows.append((d, meta))
            if len(rows) >= n:
                break
    return rows


STATE_LABELS = {"queued": "in the upload queue", "review": "waiting for your review", "expired": "not uploaded (waited too long)",
                "failed": "upload failed earlier"}


def _report_upload(job_id: str, title: str, folder: Any) -> None:
    """Tell the owner how an upload started from Telegram ended."""
    from .web import server
    job = server.JOBS.get(job_id)
    for _ in range(3600):                       # an upload of a long video can take a while
        if job is None or job.finished:
            break
        time.sleep(2)
    if job is None:
        return
    if job.status == "done":
        meta = load_json(folder / "meta.json") or {}
        vid = meta.get("youtube_id")
        when = ""
        if meta.get("publish_at") and meta.get("privacy") == "private":
            from datetime import datetime
            when = ", goes public " + datetime.fromisoformat(meta["publish_at"].replace("Z", "+00:00")).astimezone().strftime("%a %H:%M")
        text = (f"✅ Uploaded: {title[:80]}\nhttps://youtube.com/shorts/{vid} ({meta.get('privacy', 'private')}{when})"
                if vid else f"Upload of {title[:80]} finished, but no YouTube id was recorded; check the console.")
    elif job.status == "stopped":
        text = f"Upload of {title[:80]} was stopped."
    else:
        text = f"❌ Upload of {title[:80]} failed: {(job.error or 'unknown error')[:300]}"
    try:
        _say(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram reply failed: %s", exc)


def _decide(d: Any, decision: str) -> str:
    from . import upload_queue
    meta = load_json(d / "meta.json") or {}
    if not meta:
        return "That Short is gone."
    if meta.get("youtube_id"):
        return f"{meta.get('title', '')[:60]} is already on YouTube."
    upload_queue.set_approval(d, decision, "decided on Telegram")
    if decision == "approved":
        return f"✅ Approved: {meta.get('title', '')[:70]} uploads in its turn."
    return f"❌ Rejected: {meta.get('title', '')[:70]} stays on this computer and is not uploaded (delete it from its card if you want)."


def _decide_from_button(data: str) -> str:
    action, _, name = data.partition(":")
    if action not in ("approve", "reject") or not name or "/" in name or "\\" in name:
        return "Unknown button."
    return _decide(OUTPUT_DIR / name, "approved" if action == "approve" else "rejected")


def _status() -> str:
    from . import upload_queue
    from .scheduler import schedule_config, _tuning_summary
    cfg = schedule_config()
    q = upload_queue.summary()
    t = _tuning_summary() or {}
    lines = [f"Schedule: {'ON' if cfg['enabled'] else 'paused'}",
             f"Upload queue: {q['queued']} waiting, {q['uploaded_24h']}/{q['limit']} uploaded in 24 h"
             + (f", paused until {q['paused_until']}" if q.get("paused_until") else "")]
    if q.get("next"):
        lines.append(f"Next upload: {q['next']['title'][:70]} (priority {q['next']['priority']})")
    if t.get("uploads_per_day"):
        lines.append(f"Learned: {t['uploads_per_day']} uploads/day, {t['seconds']} s Shorts")
    return "\n".join(lines)


def handle(text: str) -> str:
    """Run one command and return the reply."""
    from . import comments, upload_queue
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].lower().split("@")[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""
    state = load_json(STATE_PATH) or {}
    if cmd in ("/start", "/help"):
        return __doc__.split("Commands:")[1].split("If two")[0].strip()
    if cmd == "/status":
        return _status()
    if cmd == "/queue":
        items = upload_queue.queued()
        return "\n".join(f"{i['priority']:>3}  {i['title'][:70]}" for i in items[:10]) or "Nothing waiting."
    if cmd == "/uploads":
        rows = _uploads()
        state["uploads"] = [d.name for d, _ in rows]
        save_json(STATE_PATH, state)
        from datetime import datetime
        def when(m):
            if m.get("publish_at") and m.get("privacy") == "private":
                return "goes public " + datetime.fromisoformat(m["publish_at"].replace("Z", "+00:00")).astimezone().strftime("%a %H:%M")
            return m.get("privacy", "?")
        return "\n".join(f"{i}. {m['title'][:60]} [{when(m)}]" for i, (_, m) in enumerate(rows, 1)) or "No uploads yet."
    if cmd == "/videos":
        rows = _not_uploaded()
        state["ready"] = [d.name for d, _ in rows]
        save_json(STATE_PATH, state)
        return ("\n".join(f"{i}. {m['title'][:60]} [{STATE_LABELS.get(m.get('upload_state'), 'not uploaded')}"
                           + (f", priority {m['upload_priority']}" if m.get("upload_priority") is not None else "") + "]"
                           for i, (_, m) in enumerate(rows, 1))
                + "\n\nSend /upload N to upload one now.") if rows else "Every video made is already on YouTube."
    if cmd == "/upload":
        names = state.get("ready") or []
        if not arg.isdigit() or not 1 <= int(arg) <= len(names):
            return "Send /videos first, then /upload N."
        d = OUTPUT_DIR / names[int(arg) - 1]
        meta = load_json(d / "meta.json") or {}
        if meta.get("youtube_id"):
            return f"{meta.get('title', '')[:70]} is already on YouTube: https://youtube.com/shorts/{meta['youtube_id']}"
        if not (d / "short.mp4").exists():
            return "That video's file is gone (deleted or cleaned up); it cannot be uploaded."
        from .web import server
        job = server.submit_job("upload", {"path": f"output/{d.name}"})
        if job is None:
            running = server._CURRENT.kind if server._CURRENT else "another"
            return f"Busy: a {running} job is running. Try again when it is done, or send /stop."
        threading.Thread(target=_report_upload, args=(job["id"], meta.get("title", d.name), d), daemon=True).start()
        return f"Uploading {meta.get('title', d.name)[:70]}... I'll message you when it is on YouTube."
    if cmd in ("/publish", "/private"):
        names = state.get("uploads") or []
        if not arg.isdigit() or not 1 <= int(arg) <= len(names):
            return "Send /uploads first, then /publish N or /private N."
        d = OUTPUT_DIR / names[int(arg) - 1]
        meta = load_json(d / "meta.json") or {}
        from .upload import set_privacy
        privacy = "public" if cmd == "/publish" else "private"
        set_privacy(meta["youtube_id"], privacy)
        meta["privacy"], meta["publish_at"] = privacy, None
        save_json(d / "meta.json", meta)
        return f"{meta['title'][:70]} is now {privacy}: https://youtube.com/shorts/{meta['youtube_id']}"
    if cmd == "/drafts":
        rows = [c for c in comments.pending() if c["status"] == "drafted"][:10]
        state["drafts"] = [c["id"] for c in rows]
        save_json(STATE_PATH, state)
        return "\n\n".join(f"{i}. {c['author']}: {c['text'][:120]}\n   ✍️ {c.get('draft', '')}" for i, c in enumerate(rows, 1)) \
            or "No reply drafts waiting."
    if cmd in ("/reply", "/dismiss"):
        ids = state.get("drafts") or []
        if not arg.isdigit() or not 1 <= int(arg) <= len(ids):
            return "Send /drafts first, then /reply N (optionally followed by your own text) or /dismiss N."
        tid = ids[int(arg) - 1]
        if cmd == "/dismiss":
            comments.dismiss(tid)
            return "Dismissed."
        comments.post_reply(tid, rest or None)
        return "Reply posted."
    if cmd == "/report":
        from .alerts import daily_report
        return daily_report()
    if cmd == "/stop":
        from .web import server
        if server._CURRENT is None:
            return "Nothing is running."
        kind = server._CURRENT.kind
        server.stop_job()
        return f"Stopping the {kind} job."
    if cmd in ("/approve", "/reject"):
        names = state.get("ready") or []
        if not arg.isdigit() or not 1 <= int(arg) <= len(names):
            return "Send /videos first, then /approve N or /reject N."
        return _decide(OUTPUT_DIR / names[int(arg) - 1], "approved" if cmd == "/approve" else "rejected")
    if cmd == "/update":
        from .web import server
        from . import updater
        if server._CURRENT is not None:
            return f"A {server._CURRENT.kind} job is running; send /update when it is done, or /stop first."
        return updater.apply(restart=True)["message"]
    if cmd in ("/pause", "/resume"):
        from .config import save_local_config
        save_local_config({"schedule": {"enabled": cmd == "/resume"}})
        return "Scheduled production paused." if cmd == "/pause" else "Scheduled production resumed."
    return "Unknown command. Send /help."


# ---------------------------------------------------------------------------------------------------- polling
def _loop() -> None:
    log.info("Telegram commands: listening")
    while True:
        if not enabled():
            time.sleep(60)
            continue
        state = load_json(STATE_PATH) or {}
        try:
            r = requests.get(_api("getUpdates"), params={"offset": state.get("offset", 0), "timeout": 50,
                                                          "allowed_updates": '["message","callback_query"]'}, timeout=70)
            updates = r.json().get("result", []) if r.ok else []
        except Exception as exc:  # noqa: BLE001
            log.debug("telegram poll failed: %s", exc)
            time.sleep(15)
            continue
        for u in updates:
            state = load_json(STATE_PATH) or {}
            state["offset"] = u["update_id"] + 1
            save_json(STATE_PATH, state)
            cq = u.get("callback_query")
            if cq:
                chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
                if chat == str(env("TELEGRAM_CHAT_ID")):
                    try:
                        reply = _decide_from_button(cq.get("data") or "")
                    except Exception as exc:  # noqa: BLE001
                        reply = f"That failed: {str(exc)[:200]}"
                    try:
                        requests.post(_api("answerCallbackQuery"), json={"callback_query_id": cq["id"], "text": reply[:180]}, timeout=20)
                        _say(reply)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("telegram reply failed: %s", exc)
                continue
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(env("TELEGRAM_CHAT_ID")):
                continue                                   # only the owner's chat may give commands
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            try:
                reply = handle(text)
            except Exception as exc:  # noqa: BLE001
                reply = f"That failed: {str(exc)[:300]}"
            try:
                _say(reply)
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram reply failed: %s", exc)


def start() -> None:
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_loop, name="telegram-commands", daemon=True)
        _thread.start()
