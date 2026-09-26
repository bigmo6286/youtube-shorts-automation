"""Build an ASS subtitle file with big centred word-group captions from TTS word timings."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words: list[dict[str, Any]], words_per_caption: int, full_text: str | None = None) -> list[list[dict[str, Any]]]:
    """Chunk TTS words into caption groups, never crossing a sentence end when the script text is known."""
    ends: set[int] = set()
    if full_text:
        tokens = full_text.split()
        if len(tokens) == len(words):  # edge-tts emits one boundary per whitespace token
            ends = {i for i, t in enumerate(tokens) if t.rstrip('"\')') and t.rstrip('"\')')[-1] in ".!?"}
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for i, w in enumerate(words):
        current.append(w)
        if len(current) >= words_per_caption or i in ends:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def write_ass(words: list[dict[str, Any]], out_path: Path, *, words_per_caption: int = 3, full_text: str | None = None,
              font: str = "Impact", font_size: int = 96, width: int = 1080, height: int = 1920) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{font_size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1
Style: Hi,{font},{int(font_size * 1.08)},&H0000E5FF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for group in group_words(words, words_per_caption, full_text):
        end = group[-1]["end"] + 0.05
        # one event per spoken word so the current word is highlighted, the rest of the group stays plain
        for k, w in enumerate(group):
            w_start = w["start"]
            w_end = group[k + 1]["start"] if k + 1 < len(group) else end
            parts = []
            for j, g in enumerate(group):
                t = _esc(g["text"].upper())
                parts.append(("{\\rHi}" + t + "{\\rCap}") if j == k else t)
            text = " ".join(parts)
            events.append(f"Dialogue: 0,{_ts(w_start)},{_ts(w_end)},Cap,,0,0,0,,{{\\an5\\pos({width // 2},{int(height * 0.62)})}}{text}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path
