"""Voiceover with edge-tts, returning word timings for captions."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import edge_tts


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


def synthesize(text: str, out_path: Path, *, voice: str, rate: str = "+0%") -> list[dict[str, Any]]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_synth(text, voice, rate, out_path))
