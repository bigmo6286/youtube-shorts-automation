"""Sound design: a soft whoosh on every cut and a low hit under the hook.

The two sounds are generated once with ffmpeg into assets/sfx (whoosh.wav, hit.wav); drop your own files with the
same names there to replace them. For each render a single effects track is built (whooshes a little before each
segment change, the hit where the body starts, so a looping Short hits again on replay) and mixed under the voice
and music at the levels in `production.sfx`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import ASSETS_DIR, load_config
from .tools import run as _run

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "whoosh": True, "hit": True, "whoosh_db": -17, "hit_db": -9, "min_gap": 0.8}
SFX_DIR = ASSETS_DIR / "sfx"
WHOOSH_LEAD = 0.12          # the whoosh starts this long before the cut, so its peak lands on it


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("sfx")) or {})
    return cfg


def ensure_assets() -> dict[str, Path]:
    """whoosh.wav and hit.wav, generated if missing (pink-noise sweep; a 52 Hz thump with a soft click)."""
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    whoosh, hit = SFX_DIR / "whoosh.wav", SFX_DIR / "hit.wav"
    if not whoosh.exists():
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=0.45:color=pink:seed=7:amplitude=0.7:r=48000",
              "-af", "highpass=f=350,lowpass=f=4200,afade=t=in:st=0:d=0.14:curve=qsin,afade=t=out:st=0.18:d=0.27:curve=qsin,"
                     "aformat=channel_layouts=stereo",
              str(whoosh)], check=True, text=True, timeout=60)
    if not hit.exists():
        _run(["ffmpeg", "-y", "-loglevel", "error",
              "-f", "lavfi", "-i", "sine=frequency=52:duration=0.8:sample_rate=48000",
              "-f", "lavfi", "-i", "anoisesrc=d=0.04:color=white:seed=3:amplitude=0.25:r=48000",
              "-filter_complex", "[0:a]volume=1.4,afade=t=in:st=0:d=0.01,afade=t=out:st=0.08:d=0.7:curve=exp[b];"
                                 "[1:a]lowpass=f=1800,afade=t=out:st=0:d=0.04[c];"
                                 "[b][c]amix=inputs=2:duration=longest:normalize=0,aformat=channel_layouts=stereo[o]",
              "-map", "[o]", str(hit)], check=True, text=True, timeout=60)
    return {"whoosh": whoosh, "hit": hit}


def cut_times(segments: list[dict[str, Any]], offset: float = 0.0, min_gap: float = 0.8) -> list[float]:
    """Where the background changes (segment starts after the first), shifted by the intro, at least min_gap apart."""
    out: list[float] = []
    for seg in segments[1:]:
        t = float(seg["start"]) + offset
        if t > 0.3 and (not out or t - out[-1] >= min_gap):
            out.append(round(t, 3))
    return out


def build_track(cuts: list[float], hook_at: float | None, total: float, out_path: Path,
                cfg: dict[str, Any] | None = None) -> Path | None:
    """One stereo 48 kHz track of `total` seconds with the effects in place, or None when there is nothing to add."""
    cfg = cfg or config()
    if not cfg["enabled"]:
        return None
    cuts = cuts if cfg["whoosh"] else []
    hook = hook_at if (cfg["hit"] and hook_at is not None) else None
    if not cuts and hook is None:
        return None
    assets = ensure_assets()
    inputs: list[str] = []
    filters: list[str] = []
    mixed: list[str] = []
    idx = 0
    if cuts:
        inputs += ["-i", str(assets["whoosh"])]
        n = len(cuts)
        filters.append(f"[{idx}:a]volume={float(cfg['whoosh_db'])}dB,asplit={n}" + "".join(f"[w{i}]" for i in range(n)))
        for i, t in enumerate(cuts):
            ms = max(0, int(round((t - WHOOSH_LEAD) * 1000)))
            filters.append(f"[w{i}]adelay={ms}|{ms}[wd{i}]")
            mixed.append(f"[wd{i}]")
        idx += 1
    if hook is not None:
        inputs += ["-i", str(assets["hit"])]
        ms = max(0, int(round(hook * 1000)))
        filters.append(f"[{idx}:a]volume={float(cfg['hit_db'])}dB,adelay={ms}|{ms}[hd]")
        mixed.append("[hd]")
        idx += 1
    if len(mixed) > 1:
        filters.append("".join(mixed) + f"amix=inputs={len(mixed)}:duration=longest:normalize=0[mx]")
    else:
        filters.append(f"{mixed[0]}anull[mx]")
    filters.append(f"[mx]apad=whole_dur={total:.3f},atrim=0:{total:.3f},aformat=sample_rates=48000:channel_layouts=stereo[out]")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
          "-c:a", "pcm_s16le", str(out_path)], check=True, text=True, timeout=120)
    log.info("sound design: %d whoosh%s%s", len(cuts), "" if len(cuts) == 1 else "es", ", hit under the hook" if hook is not None else "")
    return out_path
