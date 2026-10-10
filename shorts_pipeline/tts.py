"""Voiceover with word timings for captions.

Main voice: edge-tts (Microsoft's online neural voices, free, exact word timings). Fallback: Kokoro-82M, a free
open-source voice that runs on this PC (kokoro-onnx, ~120 MB in data/models/kokoro). It is used when edge-tts fails
(no audio, network), or always with `production.tts.engine: kokoro`. Kokoro gives no word timings, so each sentence
is voiced separately (exact sentence timing) and the words inside a sentence are spread by their length.
"""
from __future__ import annotations

import asyncio
import logging
import re
import wave
from pathlib import Path
from typing import Any

import edge_tts

from .config import SHARED_DIR, load_config

log = logging.getLogger(__name__)

KOKORO_DIR = SHARED_DIR / "models" / "kokoro"
KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
DEFAULTS = {"engine": "edge", "fallback": "kokoro", "kokoro_voice": "am_michael"}
SENTENCE_GAP = 0.12           # seconds of silence between Kokoro sentences


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("tts")) or {})
    return cfg


# ---------------------------------------------------------------------------------------------------- edge-tts
async def _synth(text: str, voice: str, rate: str, out_path: Path) -> list[dict[str, Any]]:
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    words: list[dict[str, Any]] = []
    with open(out_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # offsets are in 100-nanosecond ticks
                words.append({"text": chunk["text"], "start": chunk["offset"] / 1e7,
                              "end": (chunk["offset"] + chunk["duration"]) / 1e7})
    return words


def synthesize_edge(text: str, out_path: Path, *, voice: str, rate: str = "+0%") -> list[dict[str, Any]]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    words = asyncio.run(_synth(text, voice, rate, out_path))
    if not words or not out_path.exists() or out_path.stat().st_size < 1000:
        raise RuntimeError("edge-tts returned no audio")
    return words


# ---------------------------------------------------------------------------------------------------- Kokoro
_KOKORO = None


def kokoro_available() -> bool:
    if not all((KOKORO_DIR / f).exists() for f in KOKORO_FILES):
        return False
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def download_kokoro() -> Path:
    """Fetch the Kokoro model files (~120 MB) into data/models/kokoro (shared by every channel)."""
    import requests
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        dest = KOKORO_DIR / name
        if dest.exists() and dest.stat().st_size > 1_000_000:
            continue
        log.info("downloading %s", name)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(dest)
    return KOKORO_DIR


def _kokoro():
    global _KOKORO
    if _KOKORO is None:
        from kokoro_onnx import Kokoro
        _KOKORO = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.int8.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
    return _KOKORO


def _speed(rate: str) -> float:
    m = re.match(r"([+-]?\d+)%", rate or "")
    return max(0.6, min(1.6, 1.0 + int(m.group(1)) / 100)) if m else 1.0


def _speech_span(samples, sr: int) -> tuple[float, float]:
    """Start and end of speech inside a clip (skips the quiet lead-in and tail)."""
    import numpy as np
    level = np.abs(samples)
    thr = max(0.01, float(level.max()) * 0.05) if level.size else 0.01
    idx = np.nonzero(level > thr)[0]
    if idx.size == 0:
        return 0.0, len(samples) / sr
    return float(idx[0]) / sr, float(idx[-1] + 1) / sr


def _spread_words(sentence: str, start: float, end: float) -> list[dict[str, Any]]:
    tokens = sentence.split()
    clean = [re.sub(r"^[^\w']+|[^\w']+$", "", t) for t in tokens]
    spoken = [len(c) + 1 for c in clean]
    pauses = [3 if re.search(r"[,;:]$", t) else 0 for t in tokens]      # the voice pauses after a comma
    total = float(sum(spoken) + sum(pauses)) or 1.0
    unit = (end - start) / total
    out, t = [], start
    for c, w, p in zip(clean, spoken, pauses):
        if c:
            out.append({"text": c, "start": round(float(t), 3), "end": round(float(t + w * unit * 0.92), 3)})
        t += (w + p) * unit
    return out


def synthesize_kokoro(text: str, out_path: Path, *, voice: str | None = None, rate: str = "+0%") -> list[dict[str, Any]]:
    import numpy as np
    from .tools import run

    voice = voice or config()["kokoro_voice"]
    k = _kokoro()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text).strip()) if s.strip()]
    pieces, words, t, sr = [], [], 0.0, 24000
    for sent in sentences:
        samples, sr = k.create(sent, voice=voice, speed=_speed(rate), lang="en-us")
        samples = np.asarray(samples, dtype=np.float32)
        s0, s1 = _speech_span(samples, sr)
        words += _spread_words(sent, float(t + s0), float(t + s1))
        pieces += [samples, np.zeros(int(SENTENCE_GAP * sr), dtype=np.float32)]
        t += len(samples) / sr + SENTENCE_GAP
    audio = np.concatenate(pieces) if pieces else np.zeros(sr, dtype=np.float32)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wav = out_path.with_suffix(".kokoro.wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "128k", str(out_path)],
        check=True, text=True)
    wav.unlink(missing_ok=True)
    return words


# ---------------------------------------------------------------------------------------------------- entry point
def synthesize(text: str, out_path: Path, *, voice: str, rate: str = "+0%") -> list[dict[str, Any]]:
    """The configured voice, falling back to the free local Kokoro voice when edge-tts fails."""
    cfg = config()
    if cfg["engine"] == "kokoro" and kokoro_available():
        log.info("voice: Kokoro (%s), free local model", cfg["kokoro_voice"])
        return synthesize_kokoro(text, out_path, rate=rate)
    try:
        return synthesize_edge(text, out_path, voice=voice, rate=rate)
    except Exception as exc:  # noqa: BLE001
        if cfg.get("fallback") == "kokoro" and kokoro_available() and voice.lower().startswith("en-"):
            log.warning("edge-tts failed (%s); voicing with the free local Kokoro voice instead", str(exc)[:120])
            return synthesize_kokoro(text, out_path, rate=rate)
        raise
