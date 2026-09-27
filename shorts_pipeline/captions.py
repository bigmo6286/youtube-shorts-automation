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


def _header(font: str, font_size: int, width: int, height: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{font_size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1
Style: Hi,{font},{int(font_size * 1.08)},&H0000E5FF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1
Style: Card,{font},{int(font_size * 1.15)},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,8,4,5,90,90,0,1
Style: CardSub,{font},{int(font_size * 0.7)},&H0000E5FF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,3,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_card_ass(text: str, seconds: float, out_path: Path, *, sub_text: str = "", font: str = "Impact",
                   font_size: int = 96, width: int = 1080, height: int = 1920) -> Path:
    """A title / end card: big centred text that fades in and out over `seconds`."""
    fade_ms = int(min(400, seconds * 1000 / 3))
    events = [f"Dialogue: 0,{_ts(0)},{_ts(seconds)},Card,,0,0,0,,{{\\an5\\pos({width // 2},{int(height * 0.46)})\\fad({fade_ms},{fade_ms})}}{_esc(text.upper())}"]
    if sub_text:
        events.append(f"Dialogue: 0,{_ts(0)},{_ts(seconds)},CardSub,,0,0,0,,{{\\an5\\pos({width // 2},{int(height * 0.58)})\\fad({fade_ms},{fade_ms})}}{_esc(sub_text)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_header(font, font_size, width, height) + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def write_ass(words: list[dict[str, Any]], out_path: Path, *, words_per_caption: int = 3, full_text: str | None = None,
              offset: float = 0.0, font: str = "Impact", font_size: int = 96, width: int = 1080, height: int = 1920) -> Path:
    """Word-highlight captions. `offset` shifts every timing (the intro card plays before the voice starts)."""
    header = _header(font, font_size, width, height)
    events = []
    for group in group_words(words, words_per_caption, full_text):
        end = group[-1]["end"] + 0.05 + offset
        # one event per spoken word so the current word is highlighted, the rest of the group stays plain
        for k, w in enumerate(group):
            w_start = w["start"] + offset
            w_end = group[k + 1]["start"] + offset if k + 1 < len(group) else end
            parts = []
            for j, g in enumerate(group):
                t = _esc(g["text"].upper())
                parts.append(("{\\rHi}" + t + "{\\rCap}") if j == k else t)
            text = " ".join(parts)
            events.append(f"Dialogue: 0,{_ts(w_start)},{_ts(w_end)},Cap,,0,0,0,,{{\\an5\\pos({width // 2},{int(height * 0.62)})}}{text}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path
