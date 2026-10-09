"""Compose the final 1080x1920 MP4 with ffmpeg: background segments + voice + music + captions."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .captions import subtitles_filter
from .tools import run as _run, ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()

W, H = 1080, 1920

# ---------------------------------------------------------------------------------------------------- video encoder
# Hardware encoders, tried in this order; the first that really works on this machine is remembered. Filtering
# (scale, crop, captions) stays on the CPU, so the gain is in the encode step only: small on a weak laptop with
# Intel Quick Sync, larger with an NVIDIA card (NVENC). libx264 is always the fallback.
ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "21", "-preset", "veryfast"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "21", "-qp_p", "23"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"],
}


def _encoder_state_path() -> Path:
    from .config import SHARED_DIR
    return SHARED_DIR / "encoder.json"


def probe_encoder(force: bool = False) -> str:
    """The encoder to use: production.encoder, or (auto) the first hardware encoder that encodes a test clip here."""
    from .config import load_config
    from .storage import load_json, save_json
    wanted = ((load_config().get("production") or {}).get("encoder") or "auto").strip()
    if wanted != "auto":
        return wanted if wanted in ENCODERS else "libx264"
    state = load_json(_encoder_state_path()) or {}
    if state.get("encoder") and not force:
        return state["encoder"]
    import tempfile
    chosen = "libx264"
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
            out = Path(tmp) / f"{name}.mp4"
            r = _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30",
                      "-t", "1", "-pix_fmt", "yuv420p", *ENCODERS[name], str(out)], text=True, retries=0)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                chosen = name
                break
    save_json(_encoder_state_path(), {"encoder": chosen})
    return chosen


def _forget_encoder() -> None:
    from .storage import save_json
    save_json(_encoder_state_path(), {"encoder": "libx264", "hardware_failed": True})


def probe_duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
               capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def probe_channels(path: Path) -> int:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    streams = json.loads(out).get("streams") or [{}]
    return int(streams[0].get("channels") or 2)


def _vchain(idx: int, length: float, label: str) -> str:
    return (f"[{idx}:v]trim=duration={length:.3f},setpts=PTS-STARTPTS,"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,format=yuv420p,setsar=1[{label}]")


def render(segments: list[dict[str, Any]], voice_path: Path, ass_path: Path, out_path: Path, *,
           total_seconds: float, music_path: Path | None = None, music_volume_db: float = -18.0,
           duck: bool = True, fade_seconds: float = 1.5,
           intro: dict[str, Any] | None = None, outro: dict[str, Any] | None = None) -> Path:
    """Segments are {path, start, end}; each is trimmed/looped to its slot, scaled and cropped to 9:16.
    Music (optional) is looped to the video length, faded in and out, and ducked under the voice.
    intro / outro are {path, seconds, ass?}: a card or clip placed before / after the body; the voice is
    delayed by the intro and `total_seconds` must already include both. Clips are muted (music plays over them)."""
    work = out_path.parent.resolve()
    inputs: list[str] = []
    filters: list[str] = []
    idx = 0
    parts: list[str] = []

    if intro:
        inputs += ["-stream_loop", "-1", "-i", str(Path(intro["path"]).resolve())]
        filters.append(_vchain(idx, intro["seconds"], "vintro_raw"))
        filters.append(f"[vintro_raw]{subtitles_filter(Path(intro['ass']))}[vintro]" if intro.get("ass") else "[vintro_raw]null[vintro]")
        parts.append("[vintro]")
        idx += 1

    body_labels = []
    for seg in segments:
        length = max(0.5, seg["end"] - seg["start"])
        inputs += ["-stream_loop", "-1", "-i", str(Path(seg["path"]).resolve())]
        filters.append(_vchain(idx, length, f"v{idx}"))
        body_labels.append(f"[v{idx}]")
        idx += 1
    filters.append("".join(body_labels) + f"concat=n={len(body_labels)}:v=1:a=0[vcat]")
    # captions: run ffmpeg from the work dir so the ASS path needs no Windows drive-letter escaping
    filters.append(f"[vcat]{subtitles_filter(ass_path)}[vbody]")
    parts.append("[vbody]")

    if outro:
        inputs += ["-stream_loop", "-1", "-i", str(Path(outro["path"]).resolve())]
        filters.append(_vchain(idx, outro["seconds"], "voutro_raw"))
        filters.append(f"[voutro_raw]{subtitles_filter(Path(outro['ass']))}[voutro]" if outro.get("ass") else "[voutro_raw]null[voutro]")
        parts.append("[voutro]")
        idx += 1

    if len(parts) > 1:
        filters.append("".join(parts) + f"concat=n={len(parts)}:v=1:a=0[vout]")
    else:
        filters.append("[vbody]null[vout]")

    voice_idx = idx
    inputs += ["-i", str(voice_path.resolve())]
    intro_ms = int(round((intro["seconds"] if intro else 0.0) * 1000))
    voice_pre = f"adelay={intro_ms}|{intro_ms}," if intro_ms else ""
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
        filters.append(voice_chain + f",{voice_pre}apad=whole_dur={total_seconds:.3f},asplit=2[vo][vsc]")
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
    elif intro_ms or outro:
        filters.append(f"[{voice_idx}:a]{voice_pre}apad=whole_dur={total_seconds:.3f}[aout]")
        amap = "[aout]"
    else:
        amap = f"{voice_idx}:a"

    encoder = probe_encoder()

    def command(enc: str) -> list[str]:
        return ["ffmpeg", "-y", "-loglevel", "error", *inputs,
                "-filter_complex", ";".join(filters),
                "-map", "[vout]", "-map", amap,
                *ENCODERS[enc], "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                "-t", f"{total_seconds + 0.3:.3f}", out_path.name]
    try:
        _run(command(encoder), check=True, cwd=str(work), text=True)
    except RuntimeError:
        if encoder == "libx264":
            raise
        import logging
        logging.getLogger(__name__).warning("hardware encoder %s failed on this render; using libx264 from now on", encoder)
        _forget_encoder()
        _run(command("libx264"), check=True, cwd=str(work), text=True)
    return out_path.resolve()
