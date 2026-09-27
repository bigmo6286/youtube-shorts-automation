"""Compose the final 1080x1920 MP4 with ffmpeg: background segments + voice + music + captions."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .tools import ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()

W, H = 1080, 1920


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def probe_channels(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    streams = json.loads(out).get("streams") or [{}]
    return int(streams[0].get("channels") or 2)


def render(segments: list[dict[str, Any]], voice_path: Path, ass_path: Path, out_path: Path, *,
           total_seconds: float, music_path: Path | None = None, music_volume_db: float = -18.0,
           duck: bool = True, fade_seconds: float = 1.5) -> Path:
    """Segments are {path, start, end}; each is trimmed/looped to its slot, scaled and cropped to 9:16.
    Music (optional) is looped to the video length, faded in and out, and ducked under the voice."""
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
    if music_path and Path(music_path).exists():
        fade = max(0.0, min(fade_seconds, total_seconds / 3))
        inputs += ["-stream_loop", "-1", "-i", str(Path(music_path).resolve())]
        music_chain = (f"[{voice_idx + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"atrim=duration={total_seconds:.3f},asetpts=PTS-STARTPTS,volume={music_volume_db}dB")
        if fade > 0:
            music_chain += f",afade=t=in:st=0:d={fade:.2f},afade=t=out:st={max(0.0, total_seconds - fade):.3f}:d={fade:.2f}"
        filters.append(music_chain + "[m]")
        # edge-tts voices are mono; aformat's mono->stereo upmix drops 3 dB, so duplicate the channel at unity
        if probe_channels(voice_path) == 1:
            voice_chain = f"[{voice_idx}:a]aformat=sample_rates=48000,pan=stereo|c0=c0|c1=c0"
        else:
            voice_chain = f"[{voice_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo"
        filters.append(voice_chain + ",asplit=2[vo][vsc]")
        if duck:
            # sidechaincompress lowers the music while the voice is loud, so words stay intelligible
            filters.append("[m][vsc]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=500:makeup=1[md]")
            mix_in = "[vo][md]"
        else:
            filters.append("[vsc]anullsink")
            mix_in = "[vo][m]"
        # normalize=0: keep the voice at its own level instead of amix halving every input
        filters.append(f"{mix_in}amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
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
