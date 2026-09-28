"""Thumbnail: a still from the first footage clip, a dark lower band, and a punchy 3-6 word line."""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from .captions import POSITIONS, _ass_color, _esc, _header, resolve_style, subtitles_filter
from .render import probe_duration
from .tools import ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()
log = logging.getLogger(__name__)

W, H = 1080, 1920


def thumbnail_text(script: dict[str, Any], max_words: int = 6) -> str:
    text = (script.get("thumbnail_text") or "").strip()
    if not text:
        words = re.sub(r"[^\w' ]+", " ", script.get("title") or "").split()
        text = " ".join(words[:max_words])
    return text


def _thumb_ass(text: str, handle: str, style: dict[str, Any], out_path: Path) -> Path:
    st = dict(style)
    st["size"] = int(int(style["size"]) * 1.45)           # thumbnail text reads at feed size
    header = _header(st, W, H)
    y = int(H * 0.68)
    lines = [f"Dialogue: 0,0:00:00.00,0:00:05.00,Card,,0,0,0,,{{\\an5\\pos({W // 2},{y})}}{_esc(text.upper() if st.get('uppercase') else text)}"]
    if handle:
        lines.append(f"Dialogue: 0,0:00:00.00,0:00:05.00,CardSub,,0,0,0,,{{\\an5\\pos({W // 2},{int(H * 0.90)})}}{_esc(handle)}")
    out_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def source_for_output(out_dir: Path, meta: dict[str, Any] | None) -> tuple[Path | None, float]:
    """Best still source for an existing output: its recorded first footage clip, else the rendered video
    just after the intro (captions are burned in there, so the band is made more opaque by the caller)."""
    segs = (meta or {}).get("segments") or []
    if segs and Path(segs[0]["path"]).exists():
        return Path(segs[0]["path"]), -1.0
    video = out_dir / "short.mp4"
    if video.exists():
        # the body ends 0.4 s after the last spoken word, so that moment has footage but no caption;
        # the outro card (2 s by default) follows it
        try:
            total = probe_duration(video)
        except Exception:  # noqa: BLE001
            total = float((meta or {}).get("duration") or 0)
        outro = 2.0 if (meta or {}).get("outro") else 0.0
        return video, max(0.0, total - outro - 0.22)
    return None, 0.0


def make_thumbnail(script: dict[str, Any], segments: list[dict[str, Any]], out_dir: Path, *,
                   style: dict[str, Any] | None = None, handle: str = "", band: bool = True,
                   source: Path | None = None, seek_at: float = -1.0, band_alpha: float = 0.7) -> Path | None:
    """Write out_dir/thumbnail.jpg (1080x1920). Returns None if anything fails; a thumbnail is optional."""
    if source is not None:
        src = source
    elif segments:
        src = Path(segments[0]["path"])
    else:
        return None
    if not src.exists():
        return None
    st = resolve_style(style)
    text = thumbnail_text(script)
    ass = _thumb_ass(text, handle, st, out_dir / "thumbnail.ass")
    if seek_at >= 0:
        seek = seek_at
    else:
        try:
            seek = max(0.0, min(1.0, probe_duration(src) / 2))
        except Exception:  # noqa: BLE001
            seek = 0.5
    base = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},eq=contrast=1.08:saturation=1.15"
    inputs = ["-ss", f"{seek:.2f}", "-i", str(src.resolve())]
    if band:
        # soft dark gradient over the lower half so the text reads on any footage
        inputs += ["-f", "lavfi", "-i",
                   f"color=black:s={W}x{H}:d=1,format=rgba,"
                   f"geq=r=0:g=0:b=0:a='if(lt(Y,{int(H * 0.40)}),0,min(255,255*(Y-{int(H * 0.40)})/{int(H * 0.30)}))*{band_alpha:.2f}'"]
        graph = base + "[b];[b][1:v]overlay=0:0:format=auto[g];[g]" + subtitles_filter(ass) + "[out]"
    else:
        graph = base + "[g];[g]" + subtitles_filter(ass) + "[out]"
    out = out_dir / "thumbnail.jpg"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
           "-frames:v", "1", "-update", "1", "-q:v", "3", out.name]
    try:
        subprocess.run(cmd, check=True, cwd=str(out_dir.resolve()))
    except subprocess.CalledProcessError as exc:
        log.warning("thumbnail failed: %s", exc)
        return None
    if out.stat().st_size > 2 * 1024 * 1024:                # YouTube limit is 2 MB
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out.name, "-q:v", "8", "-update", "1", out.name],
                       cwd=str(out_dir.resolve()))
    log.info("thumbnail: %r on %s", text, src.name)
    return out
