"""Final-render check: look at the finished video before it goes anywhere.

ffmpeg finds black stretches (a missing clip, a failed segment). TypeSafe images looks at a few frames for captions
covering a face or the main subject, text cut off at the frame edge, and blank frames. The findings are saved on
the Short's card; a severe problem (black video, blank frames) holds the Short for review instead of auto-upload.

Self-healing (`heal`): before holding anything, `plan_fix` turns the findings into a fix the producer applies and
renders again, up to `max_retries` times: a blank or black stretch means the footage of that line is picked again
(a clip not yet used in the Short, else an AI image or the generated background); captions over the subject move
the captions one position lower; text cut off at the edge shrinks the captions. Only a problem that survives the
retries holds the Short.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import has_typesafe, load_config

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "frames": [0.15, 0.5, 0.85], "black_seconds_severe": 1.0, "cover_warn": 0.7, "blank_severe": 0.7,
            "heal": True, "max_retries": 2}
LOWER = {"top": "center", "center": "lower", "lower": "bottom", "bottom": "bottom"}
SHRINK = 0.88


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("render_check")) or {})
    return cfg


def black_seconds(video: Path) -> float:
    """Total seconds of (near) black picture, from ffmpeg's blackdetect."""
    from .tools import run
    try:
        proc = run(["ffmpeg", "-hide_banner", "-i", str(video), "-vf", "blackdetect=d=0.3:pic_th=0.97", "-an", "-f", "null", "-"],
                   capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        log.debug("blackdetect failed: %s", exc)
        return 0.0
    return round(sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", (proc.stderr or "") + (proc.stdout or ""))), 2)


def black_spans(video: Path) -> list[tuple[float, float]]:
    """(start, end) of every black stretch of 0.3 s or more."""
    from .tools import run
    try:
        proc = run(["ffmpeg", "-hide_banner", "-i", str(video), "-vf", "blackdetect=d=0.3:pic_th=0.97", "-an", "-f", "null", "-"],
                   capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        log.debug("blackdetect failed: %s", exc)
        return []
    text = (proc.stderr or "") + (proc.stdout or "")
    return [(round(float(a), 2), round(float(b), 2)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", text)]


def plan_fix(result: dict[str, Any], segments: list[dict[str, Any]], duration: float, intro_seconds: float,
             style: dict[str, Any]) -> dict[str, Any] | None:
    """What to change before rendering again: {segments: [indices to re-pick], style: {...}, reasons: [...]}, or None."""
    cfg = config()
    fix: dict[str, Any] = {"segments": [], "style": {}, "reasons": []}

    def seg_at(t_body: float) -> int | None:
        for i, s in enumerate(segments):
            if float(s["start"]) - 0.05 <= t_body < float(s["end"]):
                return i
        return None

    def want(i: int | None, why: str) -> None:
        if i is not None and i not in fix["segments"]:
            fix["segments"].append(i)
            fix["reasons"].append(f"{why}: footage of line {int(segments[i].get('line', i)) + 1} picked again")

    for f in result.get("frames") or []:
        if float(f.get("blank", 0)) >= float(cfg["blank_severe"]):
            want(seg_at(float(f["at"]) * duration - intro_seconds), f"blank frame at {int(float(f['at']) * 100)}%")
    for a, b in result.get("black_spans") or []:
        for i, s in enumerate(segments):
            lo, hi = max(a - intro_seconds, float(s["start"])), min(b - intro_seconds, float(s["end"]))
            if hi - lo >= 0.3:
                want(i, f"black picture {a:.1f}-{b:.1f}s")
    issues = result.get("issues") or []
    if any("cover the subject" in x for x in issues):
        pos = style.get("position", "lower")
        if LOWER.get(pos, pos) != pos:
            fix["style"]["position"] = LOWER[pos]
            fix["reasons"].append(f"captions cover the subject: moved from {pos} to {LOWER[pos]}")
        else:
            fix["style"]["size"] = int(int(style.get("size", 84)) * SHRINK)
            fix["reasons"].append("captions cover the subject: made smaller")
    if any("cut off" in x for x in issues):
        fix["style"]["size"] = int(int(fix["style"].get("size", style.get("size", 84))) * SHRINK)
        fix["reasons"].append("text cut off at the edge: captions made smaller")
    return fix if fix["segments"] or fix["style"] else None


def _frame(video: Path, at: float, duration: float) -> bytes | None:
    from .tools import run
    from .vision import prepare
    tmp = video.with_name(f".check_{int(at * 100)}.jpg")
    try:
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0.0, at * duration):.2f}", "-i", str(video), "-frames:v", "1", str(tmp)],
            check=True, text=True, timeout=120)
        return prepare(tmp.read_bytes())
    except Exception as exc:  # noqa: BLE001
        log.debug("frame grab failed: %s", exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)


def check(video: Path, duration: float, title: str = "") -> dict[str, Any]:
    cfg = config()
    result: dict[str, Any] = {"black_seconds": black_seconds(video), "frames": [], "issues": [], "severity": "ok"}
    result["black_spans"] = black_spans(video) if result["black_seconds"] > 0 else []
    if result["black_seconds"] >= float(cfg["black_seconds_severe"]):
        result["issues"].append(f"{result['black_seconds']:.1f} s of black picture")
    if has_typesafe():
        frames = [(at, _frame(video, at, duration)) for at in cfg["frames"]]
        frames = [(at, f) for at, f in frames if f]
        if frames:
            try:
                result["frames"] = _judge_frames(frames, title)
            except Exception as exc:  # noqa: BLE001
                log.warning("render check: frames not judged (%s)", str(exc)[:120])
    covers = [f for f in result["frames"] if f["covers_subject"] >= float(cfg["cover_warn"])]
    blanks = [f for f in result["frames"] if f["blank"] >= float(cfg["blank_severe"])]
    cut = [f for f in result["frames"] if f["text_cut"] >= float(cfg["cover_warn"])]
    if len(covers) >= 2:
        result["issues"].append("captions cover the subject in most frames")
    if cut:
        result["issues"].append("text is cut off at the frame edge")
    if blanks:
        result["issues"].append(f"blank frame at {', '.join(f'{int(f['at'] * 100)}%' for f in blanks)}")
    if blanks or result["black_seconds"] >= float(cfg["black_seconds_severe"]):
        result["severity"] = "severe"
    elif result["issues"]:
        result["severity"] = "warn"
    return result


def _judge_frames(frames: list[tuple[float, bytes]], title: str) -> list[dict[str, Any]]:
    from typesafe_sdk import Image, Noul, TypeSafeClient
    from .vision import HEADERS
    state = {"video_title": title, "frames": {f"f{i}": Image(data) for i, (_, data) in enumerate(frames)}}
    questions: dict[str, Any] = {}
    for i, _ in enumerate(frames):
        questions[f"cover_{i}"] = Noul(instructions=f"In `frames.f{i}`, does the on-screen caption text cover a face, an animal's "
                                                    "head or the main subject so that it is hard to see?")
        questions[f"cut_{i}"] = Noul(instructions=f"In `frames.f{i}`, is any caption text cut off at the edge of the frame or "
                                                  "running off screen?")
        questions[f"blank_{i}"] = Noul(instructions=f"Is `frames.f{i}` essentially blank: mostly black, a solid colour or a "
                                                    "featureless gradient with no footage or picture behind the text?")
    with TypeSafeClient(timeout=90.0) as client:
        resp = client.system_one(state=state, questions=questions, model="jev-1.14.0", extra_headers=HEADERS)
    return [{"at": at, "covers_subject": round(float(resp.answers[f"cover_{i}"].noul), 2),
             "text_cut": round(float(resp.answers[f"cut_{i}"].noul), 2), "blank": round(float(resp.answers[f"blank_{i}"].noul), 2)}
            for i, (at, _) in enumerate(frames)]
