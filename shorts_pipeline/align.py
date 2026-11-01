"""Word timings for the local voice: Kokoro gives none, so a small offline recogniser (Vosk) listens to what it said.

Vosk's small English model (~40 MB, fetched once into data/models) returns each recognised word with its start and
end. The recognised words are matched to the script's words (difflib on normalised tokens); matched words take the
recognised times, the rest are spread between their timed neighbours. Captions then highlight the word really being
spoken instead of an even spread per sentence. Any failure falls back to the spread timings.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from .config import SHARED_DIR, load_config

log = logging.getLogger(__name__)

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_DIR = SHARED_DIR / "models" / MODEL_NAME


def enabled() -> bool:
    return bool(((load_config().get("production") or {}).get("tts") or {}).get("align", True))


def available() -> bool:
    try:
        import vosk  # noqa: F401
    except ImportError:
        return False
    return (MODEL_DIR / "am").exists() or (MODEL_DIR / "conf").exists()


def ensure_model() -> bool:
    """The recogniser and its model, fetching the model the first time."""
    if available():
        return True
    try:
        import vosk  # noqa: F401
    except ImportError:
        log.info("word alignment needs the vosk package: pip install -r requirements.txt")
        return False
    try:
        import requests
        MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
        zip_path = MODEL_DIR.parent / f"{MODEL_NAME}.zip"
        part = zip_path.with_suffix(".zip.part")
        if not (zip_path.exists() and zipfile.is_zipfile(zip_path)):
            log.info("fetching the offline speech model for word timings (Vosk, ~40 MB, once)")
            # resumes a download a flaky connection cut short; the archive is only kept once it is complete
            have = part.stat().st_size if part.exists() else 0
            with requests.get(MODEL_URL, stream=True, timeout=120, headers={"Range": f"bytes={have}-"} if have else None) as r:
                r.raise_for_status()
                mode = "ab" if (have and r.status_code == 206) else "wb"
                with open(part, mode) as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if not zipfile.is_zipfile(part):
                raise RuntimeError("the download is incomplete; it resumes next time")
            part.replace(zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(MODEL_DIR.parent)
        zip_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch the speech model: %s", str(exc)[:160])
        return False
    return available()


def recognize(audio_path: Path) -> list[dict[str, Any]]:
    """Recognised words with times from any audio file (converted to 16 kHz mono PCM for the recogniser)."""
    import wave
    from vosk import KaldiRecognizer, Model, SetLogLevel
    from .tools import run
    SetLogLevel(-1)
    pcm = audio_path.with_suffix(".align.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio_path), "-ac", "1", "-ar", "16000", "-f", "wav", str(pcm)],
        check=True, text=True, timeout=300)
    out: list[dict[str, Any]] = []
    try:
        rec = KaldiRecognizer(Model(str(MODEL_DIR)), 16000)
        rec.SetWords(True)
        with wave.open(str(pcm), "rb") as wf:
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    out += json.loads(rec.Result()).get("result") or []
        out += json.loads(rec.FinalResult()).get("result") or []
    finally:
        pcm.unlink(missing_ok=True)
    return [{"word": w["word"], "start": float(w["start"]), "end": float(w["end"])} for w in out]


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9']", "", token.lower())


def align(text: str, recognized: list[dict[str, Any]], fallback: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Script words with times from the recognition. Unmatched words are spread between their timed neighbours; when
    fewer than half the words match, `fallback` (the spread timings) is returned instead."""
    tokens = text.split()
    clean = [re.sub(r"^[^\w']+|[^\w']+$", "", t) for t in tokens]
    keep = [i for i, c in enumerate(clean) if c]
    if not keep or not recognized:
        return fallback or []
    a = [_norm(clean[i]) for i in keep]
    b = [_norm(r["word"]) for r in recognized]
    times: dict[int, tuple[float, float]] = {}
    for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            r = recognized[block.b + k]
            times[block.a + k] = (float(r["start"]), float(r["end"]))
    if len(times) < max(1, len(a) // 2):
        log.info("word alignment: only %d of %d words recognised; keeping the spread timings", len(times), len(a))
        return fallback or []
    n = len(a)
    starts: list[float | None] = [times[i][0] if i in times else None for i in range(n)]
    ends: list[float | None] = [times[i][1] if i in times else None for i in range(n)]
    # spread the unmatched runs between their timed neighbours, in proportion to word length
    i = 0
    while i < n:
        if starts[i] is not None:
            i += 1
            continue
        j = i
        while j < n and starts[j] is None:
            j += 1
        lo = ends[i - 1] if i > 0 else (recognized[0]["start"] if recognized else 0.0)
        hi = starts[j] if j < n else (recognized[-1]["end"] if recognized else lo + 0.3 * (j - i))
        lo, hi = float(lo), max(float(hi), float(lo) + 0.05 * (j - i))
        weights = [len(a[k]) + 1 for k in range(i, j)]
        unit = (hi - lo) / float(sum(weights))
        t = lo
        for k, w in zip(range(i, j), weights):
            starts[k], ends[k] = t, t + w * unit * 0.92
            t += w * unit
        i = j
    out = []
    last = 0.0
    for pos, i in enumerate(keep):
        s, e = max(float(starts[pos]), last), float(ends[pos])
        e = max(e, s + 0.04)
        out.append({"text": clean[i], "start": round(s, 3), "end": round(e, 3)})
        last = s
    return out
