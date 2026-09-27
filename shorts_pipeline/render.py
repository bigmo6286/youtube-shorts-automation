"""Compose the final 1080x1920 MP4 with ffmpeg: background segments + voice + music + captions."""
from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path
from typing import Any

from .config import ASSETS_DIR
from .tools import ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()

W, H = 1080, 1920


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def _pick_music() -> Path | None:
    music_dir = ASSETS_DIR / "music"
    tracks = sorted(p for p in music_dir.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg"))
    return random.choice(tracks) if tracks else None


def render(segments: list[dict[str, Any]], voice_path: Path, ass_path: Path, out_path: Path, *,
           music_volume_db: float = -18.0, total_seconds: float) -> Path:
    """Segments are {path, start, end}; each is trimmed/looped to its slot, scaled and cropped to 9:16."""
    work = out_path.parent.resolve()
    inputs: list[str] = []
    filters: list[str] = []
    for i, seg in enumerate(segments):
        length = max(0.5, seg["end"] - seg["start"])
        inputs += ["-stream_loop", "-1", "-i", str(Path(seg["path"]).resolve())]
        filters.append(
            f"[{i}:v]trim=duration={length:.3f},setpts=PTS-STARTPTS,"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"fps=30,format=yuv420p,setsar=1[v{i}]"
        )
    n = len(segments)
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]"
    filters.append(concat)
    # captions: run ffmpeg from the work dir so the ASS path needs no Windows drive-letter escaping
    filters.append(f"[vcat]subtitles={ass_path.name}[vout]")

    voice_idx = n
    inputs += ["-i", str(voice_path.resolve())]
    music = _pick_music()
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music.resolve())]
        filters.append(f"[{voice_idx + 1}:a]volume={music_volume_db}dB,atrim=duration={total_seconds:.3f}[m]")
        filters.append(f"[{voice_idx}:a][m]amix=inputs=2:duration=first:dropout_transition=2[aout]")
        amap = "[aout]"
    else:
        amap = f"{voice_idx}:a"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
           "-filter_complex", ";".join(filters),
           "-map", "[vout]", "-map", amap,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           "-t", f"{total_seconds + 0.3:.3f}", out_path.name]
    subprocess.run(cmd, check=True, cwd=str(work))
    return out_path.resolve()
