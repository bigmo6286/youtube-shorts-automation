"""Compose the final 1080x1920 MP4 with ffmpeg: background segments + voice + music + captions."""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from .captions import subtitles_filter
from .tools import run as _run, ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()

from . import frame

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


def _vchain(idx: int, length: float, label: str, motion: str = "") -> str:
    W, H = frame.size()
    return (f"[{idx}:v]trim=duration={length:.3f},setpts=PTS-STARTPTS,"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},{motion}fps=30,format=yuv420p,setsar=1[{label}]")


PUSH_AMOUNT = 0.06          # a 6% slow push over the clip: enough to feel alive, not enough to notice
FLASH_SECONDS = 0.12        # the hook flash: the first frames fade in from white
PUNCH_AMOUNT = 0.10         # the pattern interrupt's punch-in: 10% larger at the cut, settling over PUNCH_SECONDS
PUNCH_SECONDS = 0.35

# Audio mastering (production.mastering): YouTube plays everything at about -14 LUFS and only turns loud videos down,
# so a quiet Short stays quiet. Two-pass loudnorm (measure the mix, then a linear gain to the target) keeps the
# dynamics; a light compressor, an 80 Hz high-pass and a de-esser sit on the voice before the mix.
MASTER_DEFAULTS = {"enabled": True, "target_lufs": -14.0, "true_peak": -1.5, "lra": 14.0, "voice_compress": True, "deess": True}


def _motion(i: int, length: float, amount: float = PUSH_AMOUNT, punch: bool = False) -> str:
    """A slow push in (even clips) or pull out (odd clips) over the clip's length: scale per frame, crop the middle.
    `punch`: the clip also opens 10% larger and settles in a third of a second (the pattern interrupt)."""
    W, H = frame.size()
    p = f"min(t/{max(length, 0.5):.3f},1)"
    base = (f"(1+{amount}*{p})" if i % 2 == 0 else f"(1+{amount}*(1-{p}))") if amount else "1"
    z = f"({base}*(1+{PUNCH_AMOUNT}*max(0,1-t/{PUNCH_SECONDS})))" if punch else base
    return f"scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame,crop={W}:{H},"


def measure_loudness(path: Path, m: dict[str, Any]) -> dict[str, float] | None:
    """loudnorm's first pass on a mix: input loudness, true peak, range, threshold and offset. None on silence."""
    proc = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                 f"loudnorm=I={float(m['target_lufs']):.1f}:TP={float(m['true_peak']):.1f}:LRA={float(m['lra']):.1f}:print_format=json",
                 "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    text = (proc.stderr or "") + (proc.stdout or "")
    hit = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.S)
    if not hit:
        return None
    try:
        d = json.loads(hit.group(0))
        vals = {k: float(d[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (KeyError, ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in vals.values()) or vals["input_i"] < -70:
        return None
    return vals


def master_filter(m: dict[str, Any], measured: dict[str, float] | None) -> str:
    I, TP, LRA = float(m["target_lufs"]), float(m["true_peak"]), float(m["lra"])
    if measured:
        return (f"loudnorm=I={I:.1f}:TP={TP:.1f}:LRA={LRA:.1f}:measured_I={measured['input_i']:.2f}:measured_TP={measured['input_tp']:.2f}:"
                f"measured_LRA={measured['input_lra']:.2f}:measured_thresh={measured['input_thresh']:.2f}:"
                f"offset={measured['target_offset']:.2f}:linear=true:print_format=none,aresample=48000")
    return f"loudnorm=I={I:.1f}:TP={TP:.1f}:LRA={LRA:.1f}:print_format=none,aresample=48000"


def _voice_fx(m: dict[str, Any] | None) -> str:
    if not m:
        return ""
    fx = ""
    if m.get("deess", True):
        fx += ",highpass=f=80,deesser=i=0.4:m=0.5:f=0.5"
    if m.get("voice_compress", True):
        fx += ",acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120:makeup=1.6:knee=4"
    return fx


def mix_audio(voice_path: Path, out_wav: Path, *, total_seconds: float, music_path: Path | None = None,
              music_volume_db: float = -18.0, duck: bool = True, fade_seconds: float = 1.5, intro_seconds: float = 0.0,
              sfx_path: Path | None = None, mastering: dict[str, Any] | None = None) -> Path:
    """The audio of a render as one stereo 48 kHz WAV of `total_seconds`: voice (delayed by the intro, processed when
    mastering is on), music looped, faded and ducked under the voice, effects at their own level."""
    inputs: list[str] = ["-i", str(voice_path.resolve())]
    filters: list[str] = []
    idx = 1
    intro_ms = int(round(intro_seconds * 1000))
    voice_pre = f"adelay={intro_ms}|{intro_ms}," if intro_ms else ""
    # edge-tts voices are mono; aformat's mono->stereo upmix drops 3 dB, so duplicate the channel at unity
    if probe_channels(voice_path) == 1:
        voice_chain = "[0:a]aformat=sample_rates=48000,pan=stereo|c0=c0|c1=c0"
    else:
        voice_chain = "[0:a]aformat=sample_rates=48000:channel_layouts=stereo"
    voice_chain += _voice_fx(mastering)
    has_music = bool(music_path and Path(music_path).exists())
    has_sfx = bool(sfx_path and Path(sfx_path).exists())
    if has_music:
        music_idx = idx
        idx += 1
        fade = max(0.0, min(fade_seconds, total_seconds / 3))
        inputs += ["-stream_loop", "-1", "-i", str(Path(music_path).resolve())]
        music_chain = (f"[{music_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"atrim=duration={total_seconds:.3f},asetpts=PTS-STARTPTS,volume={music_volume_db}dB")
        if fade > 0:
            music_chain += f",afade=t=in:st=0:d={fade:.2f},afade=t=out:st={max(0.0, total_seconds - fade):.3f}:d={fade:.2f}"
        filters.append(music_chain + "[m]")
    if has_sfx:
        inputs += ["-i", str(Path(sfx_path).resolve())]
        filters.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=duration={total_seconds:.3f},"
                       f"apad=whole_dur={total_seconds:.3f}[sfx]")
        idx += 1
    if has_music:
        filters.append(voice_chain + f",{voice_pre}apad=whole_dur={total_seconds:.3f},asplit=2[vo][vsc]")
        if duck:
            # sidechaincompress lowers the music while the voice is loud, so words stay intelligible
            filters.append("[m][vsc]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=500:makeup=1[md]")
            mix_in = "[vo][md]"
        else:
            filters.append("[vsc]anullsink")
            mix_in = "[vo][m]"
        # normalize=0: keep the voice at its own level instead of amix halving every input
        n_in = 3 if has_sfx else 2
        filters.append(f"{mix_in}{'[sfx]' if has_sfx else ''}amix=inputs={n_in}:duration=first:dropout_transition=0:normalize=0[aout]")
    elif has_sfx:
        filters.append(voice_chain + f",{voice_pre}apad=whole_dur={total_seconds:.3f}[vo]")
        filters.append("[vo][sfx]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        filters.append(voice_chain + f",{voice_pre}apad=whole_dur={total_seconds:.3f}[aout]")
    _run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[aout]",
          "-t", f"{total_seconds:.3f}", "-c:a", "pcm_s16le", "-ar", "48000", str(out_wav)], check=True, text=True)
    return out_wav


def render(segments: list[dict[str, Any]], voice_path: Path, ass_path: Path, out_path: Path, *,
           total_seconds: float, music_path: Path | None = None, music_volume_db: float = -18.0,
           duck: bool = True, fade_seconds: float = 1.5,
           intro: dict[str, Any] | None = None, outro: dict[str, Any] | None = None,
           polish: dict[str, Any] | None = None, sfx_path: Path | None = None,
           mastering: dict[str, Any] | None = None, report: dict[str, Any] | None = None) -> Path:
    """Segments are {path, start, end}; each is trimmed/looped to its slot, scaled and cropped to 9:16.
    Music (optional) is looped to the video length, faded in and out, and ducked under the voice.
    intro / outro are {path, seconds, ass?}: a card or clip placed before / after the body; the voice is
    delayed by the intro and `total_seconds` must already include both. Clips are muted (music plays over them).
    polish: {crossfade: seconds between body clips (the clips are extended so the timing of each line is kept),
    push_in: slow push on every clip, hook_flash: the first frames fade in from white, punch_at: body-local second
    whose clip opens with a punch-in}. sfx_path: an effects track (whooshes, hits) mixed under voice and music at its
    own level. mastering: MASTER_DEFAULTS-like dict (None = off): loudness-normalised mix, processed voice; `report`
    (a dict) receives the measured loudness."""
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

    pol = polish or {}
    xf = float(pol.get("crossfade") or 0.0) if len(segments) > 1 else 0.0
    punch_at = pol.get("punch_at")
    punch_idx = None
    if punch_at is not None:
        for i, seg in enumerate(segments):
            if float(seg["start"]) - 0.05 <= float(punch_at) < float(seg["end"]):
                punch_idx = i
    body_labels = []
    lengths = []
    for i, seg in enumerate(segments):
        length = max(0.5, seg["end"] - seg["start"])
        extra = xf if (xf and i < len(segments) - 1) else 0.0          # the fade eats into the extension, not the slot
        inputs += ["-stream_loop", "-1", "-i", str(Path(seg["path"]).resolve())]
        motion = ""
        if pol.get("push_in") or i == punch_idx:
            motion = _motion(i, length + extra, PUSH_AMOUNT if pol.get("push_in") else 0.0, punch=(i == punch_idx))
        filters.append(_vchain(idx, length + extra, f"v{idx}", motion))
        body_labels.append(f"[v{idx}]")
        lengths.append(length)
        idx += 1
    if xf and len(body_labels) > 1:
        prev, offset = body_labels[0], 0.0
        for k in range(1, len(body_labels)):
            offset += lengths[k - 1]
            out = f"[x{k}]" if k < len(body_labels) - 1 else "[vcat]"
            filters.append(f"{prev}{body_labels[k]}xfade=transition=fade:duration={xf:.3f}:offset={offset:.3f}{out}")
            prev = out
    elif len(body_labels) > 1:
        filters.append("".join(body_labels) + f"concat=n={len(body_labels)}:v=1:a=0[vcat]")
    else:
        filters.append(f"{body_labels[0]}null[vcat]")
    flash = f"fade=t=in:st=0:d={FLASH_SECONDS}:color=white," if pol.get("hook_flash") else ""
    # captions: run ffmpeg from the work dir so the ASS path needs no Windows drive-letter escaping
    filters.append(f"[vcat]{flash}{subtitles_filter(ass_path)}[vbody]")
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

    # audio: mixed first into mix.wav (voice, music, effects), measured, then normalised in the video pass
    mix_wav = work / "mix.wav"
    mix_audio(voice_path, mix_wav, total_seconds=total_seconds, music_path=music_path, music_volume_db=music_volume_db,
              duck=duck, fade_seconds=fade_seconds, intro_seconds=(intro["seconds"] if intro else 0.0), sfx_path=sfx_path,
              mastering=mastering)
    inputs += ["-i", str(mix_wav)]
    if mastering:
        measured = measure_loudness(mix_wav, mastering)
        filters.append(f"[{idx}:a]{master_filter(mastering, measured)}[aout]")
        if report is not None:
            report.update({"input_lufs": round(measured["input_i"], 1) if measured else None,
                           "input_true_peak": round(measured["input_tp"], 1) if measured else None,
                           "target": float(mastering["target_lufs"]), "linear": bool(measured)})
    else:
        filters.append(f"[{idx}:a]anull[aout]")
    amap = "[aout]"
    idx += 1

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
    mix_wav.unlink(missing_ok=True)
    return out_path.resolve()
