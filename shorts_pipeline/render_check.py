"""Final-render check: look at the finished video before it goes anywhere.

ffmpeg finds black stretches (a missing clip, a failed segment). TypeSafe images looks at a few frames for captions
covering a face or the main subject, text cut off at the frame edge, and blank frames. The findings are saved on
the Short's card; a severe problem (black video, blank frames) holds the Short for review instead of auto-upload.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import has_typesafe, load_config

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "frames": [0.15, 0.5, 0.85], "black_seconds_severe": 1.0, "cover_warn": 0.7, "blank_severe": 0.7}


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
