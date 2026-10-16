"""Check footage by what it actually shows (TypeSafe images, experimental).

The footage picker judges stock clips by their text (Pexels slugs, Pixabay tags, Commons file names). Text can be
right while the picture is wrong: "volcano" tags on a green, forested volcano for a line about Antarctica. With
`production.vision.mode` on, the clip or photo the text check picked is looked at: one preview frame goes to Jev
(`TypeSafe-Danger-Zone-Images: true`) with narrow questions, and a pick that does not show the line's subject, or
shows a setting that contradicts it, is rejected so the picker takes its next candidate.

Modes: off (default; images are still experimental at TypeSafe), log (judge and log, never reject: compare with the
text-only picker), on (reject). Each check is one image, about 4,000 input tokens; at most `max_checks` per line.
A failed request never blocks a render: the text check's pick is kept.
"""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from typing import Any

import requests

from .config import has_typesafe, load_config
from .storage import CACHE_DIR, JsonCache

log = logging.getLogger(__name__)

HEADERS = {"TypeSafe-Danger-Zone-Images": "true"}
PREVIEWS = CACHE_DIR / "pexels" / "previews"          # inside the footage cache, so its size limit covers them
_RESULTS = JsonCache("vision_checks")
DEFAULTS = {"mode": "off", "model": "jev-1.14.0", "min_shows": 0.5, "max_conflict": 0.5, "max_checks": 4}
MIN_SIDE, MAX_SIDE = 280, 1568                      # TypeSafe's limits; images outside them are rejected, not resized


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("vision")) or {})
    return cfg


def enabled() -> bool:
    return config()["mode"] in ("log", "on") and has_typesafe()


def prepare(raw: bytes) -> bytes:
    """Upright baseline JPEG within TypeSafe's size limits (longest side <= 1568, shortest >= 280)."""
    from PIL import Image as PILImage, ImageOps
    img = ImageOps.exif_transpose(PILImage.open(io.BytesIO(raw))).convert("RGB")
    w, h = img.size
    scale = min(1.0, MAX_SIDE / max(w, h))
    if min(w, h) * scale < MIN_SIDE:
        scale = MIN_SIDE / min(w, h)
    if scale != 1.0:
        img = img.resize((max(MIN_SIDE, round(w * scale)), max(MIN_SIDE, round(h * scale))))
        w, h = img.size
        if max(w, h) > MAX_SIDE:                      # extreme panoramas: shrink, then pad the short side
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            canvas = PILImage.new("RGB", (max(MIN_SIDE, img.width), max(MIN_SIDE, img.height)))
            canvas.paste(img, ((canvas.width - img.width) // 2, (canvas.height - img.height) // 2))
            img = canvas
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, progressive=False)
    return out.getvalue()


def _frame_from_video(path: Path) -> bytes | None:
    from .tools import run
    tmp = path.with_suffix(".frame.jpg")
    try:
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(path), "-frames:v", "1", str(tmp)],
            check=True, text=True)
        return tmp.read_bytes()
    except Exception:  # noqa: BLE001
        return None
    finally:
        tmp.unlink(missing_ok=True)


def preview(candidate: dict[str, Any], clip: Path | None = None) -> bytes | None:
    """One frame of the candidate: its preview image (Pexels/Pixabay give one), the photo itself, or a frame of the
    downloaded clip."""
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    cached = PREVIEWS / f"{candidate['id']}.jpg"
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()
    raw = None
    url = candidate.get("preview") or (candidate["link"] if candidate.get("kind") == "photo" else None)
    if url:
        try:
            from .commons import USER_AGENT
            r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            raw = r.content
        except Exception as exc:  # noqa: BLE001
            log.debug("preview download failed for %s: %s", candidate["id"], exc)
    if raw is None and clip is not None and clip.exists():
        raw = _frame_from_video(clip)
    if raw is None:
        return None
    try:
        data = prepare(raw)
    except Exception as exc:  # noqa: BLE001
        log.debug("preview unreadable for %s: %s", candidate["id"], exc)
        return None
    cached.write_bytes(data)
    return data


def judge(line_text: str, subject: str, candidate: dict[str, Any], clip: Path | None = None,
          topic: str = "") -> dict[str, float] | None:
    """{'shows': P(the frame shows the line's subject), 'conflict': P(it contradicts the line's setting, place or
    animal), 'off_topic': P(it shows something that does not belong in a video about `topic`)}, or None when there is
    no frame or the request failed."""
    cfg = config()
    key = hashlib.sha1(f"v2|{candidate['id']}|{line_text}|{subject}|{topic}|{cfg['model']}".encode()).hexdigest()[:20]
    hit = _RESULTS.get(key)
    if hit:
        return hit
    data = preview(candidate, clip)
    if data is None:
        return None
    from typesafe_sdk import Image, Noul, TypeSafeClient
    state = {"spoken_line": line_text, "subject": subject, "video_topic": topic or subject, "frame": Image(data)}
    # Narrow questions. A broad "could it play without contradicting the line" let a factory pass for a church, and
    # asking about the subject *together with the line* let a man exhaling smoke pass for "something is coming out"
    # of a lion's nose: the subject is asked about on its own.
    questions = {
        "shows": Noul(instructions="Does the image `frame` visibly show `subject`?"),
        "conflict": Noul(instructions=(
            "Does the image `frame` show a setting, place or thing that contradicts `spoken_line`, for example green "
            "forest for a line set in Antarctica, a modern city for a line about an ancient ruin, or a different "
            "animal than the one named?")),
        "off_topic": Noul(instructions=(
            "Does the image `frame` show something that does not belong in a video about `video_topic`, for example "
            "a person smoking, food, a product or an office in a video about a wild animal?")),
    }
    try:
        with TypeSafeClient(timeout=90.0) as client:
            resp = client.system_one(state=state, questions=questions, model=cfg["model"], extra_headers=HEADERS)
    except Exception as exc:  # noqa: BLE001 - never block a render on an experimental check
        log.warning("image check failed for %s (%s); keeping the text check's pick", candidate["id"], str(exc)[:120])
        return None
    out = {k: float(resp.answers[k].noul) for k in ("shows", "conflict", "off_topic")}
    _RESULTS.set(key, out)
    return out


def verdict(result: dict[str, float] | None) -> bool:
    """Code owns the policy: a frame passes when it shows the subject and does not contradict the line."""
    if result is None:
        return True
    cfg = config()
    return (result["shows"] >= float(cfg["min_shows"]) and result["conflict"] < float(cfg["max_conflict"])
            and result.get("off_topic", 0.0) < float(cfg["max_conflict"]))


def audit(out_dir: Path) -> list[dict[str, Any]]:
    """Look at every shot of a Short already made (its clips must still be on disk): what would the image check
    have said about the text-only picker's choices? Works in any mode."""
    from .storage import load_json
    meta = load_json(out_dir / "meta.json") or {}
    script = load_json(out_dir / "script.json") or {}
    lines = script.get("lines") or []
    marked = [(ln.get("named_subject") or "").strip() for ln in lines if (ln.get("named_subject") or "").strip()]
    main = max(set(marked), key=marked.count) if marked else ""
    chunks = [(script.get("hook", ""), (lines[0].get("named_subject") if lines else "") or main
               or (lines[0]["visual_keyword"] if lines else ""))]
    chunks += [(ln["text"], ln.get("named_subject") or ln["visual_keyword"]) for ln in lines]
    chunks.append((script.get("cta", ""), main or script.get("visual_fallback", "")))
    chunks = [c for c in chunks if c[0].strip()]
    rows = []
    for i, seg in enumerate(meta.get("segments") or []):
        path = Path(seg["path"])
        if i >= len(chunks) or not path.exists() or path.suffix != ".mp4":
            continue
        text, subject = chunks[i]
        cand = {"id": "shot_" + hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12], "kind": "video"}
        result = judge(text, subject, cand, path, topic=meta.get("title", ""))
        rows.append({"shot": i + 1, "line": text, "subject": subject, "file": path.name, "result": result,
                     "passes": verdict(result) if result else None})
    return rows
