"""Comment engagement.

- Question comments: every script carries `comment_question`; once that Short is public on YouTube the engine posts
  it as the channel's first comment (people reply to a question far more than to a blank comment section).
- Viewer comments: new comments on the channel are read every couple of hours. TypeSafe judges each one: spam,
  needs the owner personally (business, complaints, harassment, private matters), and worth a reply. Replies are
  drafted by the script writer in the channel's voice and wait in the console (Post / Edit / Dismiss); with
  `comments.auto_reply` they are posted automatically, except anything that needs the owner, which is always only
  flagged (Telegram). Spam is ignored and counted.

Needs the youtube.force-ssl permission (Connect comments in the console, or `comments connect`).
Quota: reading costs 1 unit per 50 threads, each posted comment or reply 50 units.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "comments.json"
DEFAULTS = {"enabled": True, "post_questions": True, "auto_reply": False, "max_questions_per_run": 5,
            "max_drafts_per_run": 10}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((load_config().get("comments") or {}))
    return cfg


def connected() -> bool:
    from .upload import COMMENTS_SCOPE, has_scope
    return has_scope(COMMENTS_SCOPE)


def _service(interactive: bool = False):
    from googleapiclient.discovery import build
    from .upload import COMMENTS_SCOPE, _credentials, with_retries
    creds = with_retries(lambda: _credentials(interactive=interactive, extra_scopes=(COMMENTS_SCOPE,)), "connect to YouTube")
    return build("youtube", "v3", credentials=creds, static_discovery=True)


def _state() -> dict[str, Any]:
    return load_json(STATE_PATH) or {"threads": {}, "spam_ignored": 0}


def _save(state: dict[str, Any]) -> None:
    save_json(STATE_PATH, state)


def connect() -> None:
    _service(interactive=True)


# ---------------------------------------------------------------------------------------------------- question comments
def post_questions(yt=None) -> int:
    """Post each public Short's question as the first comment (once)."""
    cfg = config()
    if not cfg["post_questions"] or not OUTPUT_DIR.exists():
        return 0
    from .upload import with_retries
    posted = 0
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if posted >= int(cfg["max_questions_per_run"]):
            break
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if not meta or not meta.get("youtube_id") or meta.get("privacy") != "public" or meta.get("question_comment_id"):
            continue
        question = ((load_json(d / "script.json") or {}).get("comment_question") or "").strip()
        if not question:
            continue
        yt = yt or _service()
        body = {"snippet": {"videoId": meta["youtube_id"], "topLevelComment": {"snippet": {"textOriginal": question[:500]}}}}
        try:
            r = with_retries(lambda: yt.commentThreads().insert(part="snippet", body=body).execute(), "post question comment")
        except Exception as exc:  # noqa: BLE001
            log.warning("question comment on %s failed: %s", meta["youtube_id"], str(exc)[:160])
            meta["question_comment_error"] = str(exc)[:200]
            save_json(d / "meta.json", meta)
            continue
        meta = load_json(d / "meta.json") or meta
        meta["question_comment_id"] = r.get("id")
        save_json(d / "meta.json", meta)
        log.info("posted the question comment on %r: %s", meta.get("title", "")[:60], question)
        posted += 1
    return posted


# ---------------------------------------------------------------------------------------------------- viewer comments
def _own_channel_id(yt) -> str:
    from .upload import with_retries
    r = with_retries(lambda: yt.channels().list(part="id", mine=True).execute(), "read channel id")
    return r["items"][0]["id"]


def fetch_new(yt) -> list[dict[str, Any]]:
    """Top-level viewer comments on the channel that are new to us and not answered by the channel yet."""
    from .upload import with_retries
    state = _state()
    me = state.get("channel_id") or _own_channel_id(yt)
    state["channel_id"] = me
    resp = with_retries(lambda: yt.commentThreads().list(part="snippet,replies", allThreadsRelatedToChannelId=me,
                                                         maxResults=50, order="time", textFormat="plainText").execute(),
                        "read comments")
    new = []
    for th in resp.get("items", []):
        top = th["snippet"]["topLevelComment"]["snippet"]
        author = (top.get("authorChannelId") or {}).get("value")
        if author == me or th["id"] in state["threads"]:
            continue
        replies = (th.get("replies") or {}).get("comments") or []
        if any((r["snippet"].get("authorChannelId") or {}).get("value") == me for r in replies):
            continue
        new.append({"id": th["id"], "video_id": th["snippet"].get("videoId"), "author": top.get("authorDisplayName", ""),
                    "text": top.get("textDisplay") or top.get("textOriginal") or "", "published": top.get("publishedAt"),
                    "likes": int(top.get("likeCount") or 0)})
    _save(state)
    return new


def _video_context() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            meta = load_json(d / "meta.json") if d.is_dir() else None
            if meta and meta.get("youtube_id"):
                out[meta["youtube_id"]] = {"title": meta.get("title", ""),
                                           "script": ((load_json(d / "script.json") or {}).get("full_text") or "")[:900]}
    return out


def triage(items: list[dict[str, Any]], context: dict[str, dict[str, str]]) -> None:
    """TypeSafe: spam / needs the owner / worth a reply, one request per comment (independent judgments)."""
    from . import judge
    if not judge.has_typesafe():
        for it in items:
            it.update(spam=0.0, needs_owner=0.0, worth=0.5)
        return
    q = judge._q
    questions = {
        "spam": q("noul", "Is `comment.text` spam: self-promotion, links, scams, 'check my channel', bots, or gibberish?", None),
        "needs_owner": q("noul", "Does `comment.text` need the channel owner personally rather than a friendly public reply: a "
                                 "business or collaboration request, a complaint or correction about a fact in the video, "
                                 "harassment or hate, a personal or private matter, or anything risky to answer automatically?", None),
        "worth": q("noul", "Is `comment.text` a genuine reaction or question from a viewer that a short, friendly reply from "
                           "the channel would add to (not just an emoji or a single word)?", None),
    }
    with judge._client() as client:
        for it in items:
            state = {"comment": {"text": it["text"][:1000], "author": it["author"]},
                     "video": context.get(it["video_id"]) or {"title": "", "script": ""}}
            try:
                r = client.system_one(state=state, questions=questions)
                it.update(spam=float(r.answers["spam"].noul), needs_owner=float(r.answers["needs_owner"].noul),
                          worth=float(r.answers["worth"].noul))
            except Exception as exc:  # noqa: BLE001
                log.warning("comment triage failed: %s", str(exc)[:120])
                it.update(spam=0.0, needs_owner=1.0, worth=0.0)        # unsure: leave it to the owner


def draft_replies(items: list[dict[str, Any]], context: dict[str, dict[str, str]]) -> None:
    """One call to the script writer for all comments that deserve a reply."""
    if not items:
        return
    from . import script_gen
    system = ("You reply as a small faceless YouTube Shorts channel to comments on its videos. Replies are short (at most 25 "
              "words), warm, specific to what the viewer said, and factually consistent with the video's script. No links, "
              "no self-promotion, no 'subscribe', no emojis spam (one emoji at most), never argue. Output only the JSON object.")
    lines = []
    for it in items:
        v = context.get(it["video_id"]) or {}
        lines.append(f"[{it['id']}] video {v.get('title', '')!r}; script: {v.get('script', '')[:500]!r}\n"
                     f"    comment by {it['author']}: {it['text'][:500]!r}")
    prompt = "Write one reply for each comment:\n\n" + "\n".join(lines)
    schema = {"type": "object", "properties": {"replies": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "reply": {"type": "string"}}, "required": ["id", "reply"]}}},
        "required": ["replies"]}
    backend = script_gen.pick_backend(load_config()["production"].get("script_backend", "auto"))
    if backend == "ollama":
        from . import ollama_backend
        data = ollama_backend.generate_json(system, prompt, schema)
    else:
        from . import claude_code_backend
        data = claude_code_backend.generate_json(system, prompt, schema)
    by_id = {r.get("id"): (r.get("reply") or "").strip() for r in data.get("replies") or []}
    for it in items:
        it["draft"] = by_id.get(it["id"], "")[:500]


def post_reply(thread_id: str, text: str | None = None, yt=None) -> str:
    from .upload import with_retries
    state = _state()
    th = state["threads"].get(thread_id)
    if not th:
        raise RuntimeError("unknown comment")
    reply = (text or th.get("draft") or "").strip()
    if not reply:
        raise RuntimeError("empty reply")
    yt = yt or _service()
    r = with_retries(lambda: yt.comments().insert(part="snippet", body={"snippet": {"parentId": thread_id,
                                                                                     "textOriginal": reply}}).execute(),
                     "post reply")
    th.update(status="posted", reply=reply, reply_id=r.get("id"), posted_at=time.time())
    _save(state)
    return r.get("id", "")


def dismiss(thread_id: str) -> None:
    state = _state()
    if thread_id in state["threads"]:
        state["threads"][thread_id]["status"] = "dismissed"
        _save(state)


def run(interactive: bool = False) -> dict[str, Any]:
    """Post pending question comments, read new viewer comments, triage them and draft (or post) replies."""
    cfg = config()
    stats = {"questions": 0, "new": 0, "spam": 0, "needs_owner": 0, "drafted": 0, "posted": 0}
    if not cfg["enabled"]:
        return stats
    if not connected() and not interactive:
        raise RuntimeError("comments are not connected: use Connect comments in the console (or `comments connect`)")
    yt = _service(interactive)
    stats["questions"] = post_questions(yt)
    items = fetch_new(yt)
    stats["new"] = len(items)
    if not items:
        return stats
    context = _video_context()
    triage(items, context)
    to_draft = [it for it in items if it["spam"] < 0.5 and it["needs_owner"] < 0.5 and it["worth"] >= 0.5]
    try:
        draft_replies(to_draft[: int(cfg["max_drafts_per_run"])], context)
    except Exception as exc:  # noqa: BLE001 - triage results are still stored; drafts can be retried
        log.warning("reply drafting failed: %s", str(exc)[:160])
    state = _state()
    flagged = []
    for it in items:
        status = ("spam" if it["spam"] >= 0.5 else "needs_owner" if it["needs_owner"] >= 0.5
                  else "drafted" if it.get("draft") else "no_reply")
        state["threads"][it["id"]] = {**it, "status": status, "seen_at": time.time(), "title": (context.get(it["video_id"]) or {}).get("title", "")}
        if status == "spam":
            stats["spam"] += 1
            state["spam_ignored"] = int(state.get("spam_ignored", 0)) + 1
        elif status == "needs_owner":
            stats["needs_owner"] += 1
            flagged.append(it)
        elif status == "drafted":
            stats["drafted"] += 1
    _save(state)
    if cfg["auto_reply"]:
        for it in items:
            if state["threads"][it["id"]]["status"] == "drafted":
                try:
                    post_reply(it["id"], yt=yt)
                    stats["posted"] += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("auto reply failed: %s", str(exc)[:120])
    _notify(stats, flagged, cfg)
    log.info("comments: %d new, %d drafted, %d posted, %d need you, %d spam ignored, %d question comments posted",
             stats["new"], stats["drafted"], stats["posted"], stats["needs_owner"], stats["spam"], stats["questions"])
    return stats


def _notify(stats: dict[str, Any], flagged: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    from .notify import send_message, telegram_configured
    if not telegram_configured() or not (flagged or stats["drafted"]):
        return
    lines = []
    if flagged:
        lines.append(f"💬 {len(flagged)} comment(s) need you personally:")
        lines += [f"- {it['author']}: {it['text'][:200]}\n  https://www.youtube.com/watch?v={it['video_id']}&lc={it['id']}"
                  for it in flagged[:5]]
    if stats["drafted"] and not cfg["auto_reply"]:
        lines.append(f"✍️ {stats['drafted']} reply draft(s) waiting in the console (Overview -> Comments).")
    try:
        send_message("\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        log.warning("comment notification failed: %s", exc)


def pending() -> list[dict[str, Any]]:
    state = _state()
    rows = [dict(v, id=k) for k, v in state["threads"].items() if v.get("status") in ("drafted", "needs_owner")]
    return sorted(rows, key=lambda r: r.get("published") or "", reverse=True)
