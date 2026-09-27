"""Background footage: Pexels vertical stock clips when a key exists, else a generated motion background."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import requests

from .config import env
from .storage import CACHE_DIR
from .tools import ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()

log = logging.getLogger(__name__)

PEXELS_CACHE = CACHE_DIR / "pexels"


def pexels_clip(keyword: str, min_seconds: float, index: int = 0) -> Path | None:
    key = env("PEXELS_API_KEY")
    if not key:
        return None
    PEXELS_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         params={"query": keyword, "orientation": "portrait", "size": "medium", "per_page": 8},
                         headers={"Authorization": key}, timeout=20)
        r.raise_for_status()
        videos = r.json().get("videos", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("pexels search failed for %r: %s", keyword, exc)
        return None
    videos = [v for v in videos if v.get("duration", 0) >= min(min_seconds, 5)]
    if not videos:
        return None
    video = videos[index % len(videos)]
    files = sorted((f for f in video.get("video_files", []) if f.get("height") and f.get("height") >= f.get("width", 0)),
                   key=lambda f: abs((f.get("height") or 0) - 1920))
    if not files:
        return None
    url = files[0]["link"]
    dest = PEXELS_CACHE / f"{video['id']}.mp4"
    if not dest.exists():
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(1 << 16):
                        f.write(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("pexels download failed: %s", exc)
            return None
    return dest


def generated_background(out_path: Path, seconds: float, seed: int = 7) -> Path:
    """Slow animated gradient with a subtle noise grain. Always available, no key needed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    palette = [
        ("0x1b1f3b", "0x3a0f5c", "0x0b3b5c"),
        ("0x0f2027", "0x203a43", "0x2c5364"),
        ("0x3a1c71", "0xd76d77", "0xffaf7b"),
        ("0x141e30", "0x243b55", "0x0f0c29"),
    ][seed % 4]
    # No film grain here on purpose: noise defeats x264 and turns a 10 s clip into hundreds of MB.
    filt = (f"gradients=size=1080x1920:speed=0.015:nb_colors=3:c0={palette[0]}:c1={palette[1]}:c2={palette[2]}"
            f":duration={seconds:.2f}:rate=30,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", filt,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-t", f"{seconds:.2f}", str(out_path)]
    subprocess.run(cmd, check=True)
    return out_path


def plan_backgrounds(script: dict[str, Any], words: list[dict[str, Any]], total_seconds: float,
                     source: str, work_dir: Path) -> list[dict[str, Any]]:
    """One background segment per script line (or one generated clip for the whole video)."""
    segments: list[dict[str, Any]] = []
    use_pexels = source in ("auto", "pexels") and bool(env("PEXELS_API_KEY"))
    if use_pexels:
        # split the timeline by cumulative word counts of hook / lines / cta
        chunks = [script["hook"], *[ln["text"] for ln in script["lines"]], script["cta"]]
        keywords = [script["lines"][0]["visual_keyword"] if script["lines"] else "abstract",
                    *[ln["visual_keyword"] for ln in script["lines"]], "abstract background"]
        idx = 0
        t = 0.0
        for i, (chunk, kw) in enumerate(zip(chunks, keywords)):
            n = len(chunk.split())
            group = words[idx: idx + n]
            idx += n
            end = group[-1]["end"] if group else t
            if i == len(chunks) - 1:
                end = total_seconds
            clip = pexels_clip(kw, end - t, index=i)
            if clip is None:
                clip = generated_background(work_dir / f"bg_{i}.mp4", end - t + 0.5, seed=i)
            segments.append({"path": str(clip), "start": t, "end": end})
            t = end
        return segments
    clip = generated_background(work_dir / "bg.mp4", total_seconds + 0.5)
    return [{"path": str(clip), "start": 0.0, "end": total_seconds}]
