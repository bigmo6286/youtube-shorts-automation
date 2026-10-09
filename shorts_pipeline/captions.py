"""Caption styling: presets + overrides rendered to ASS subtitle files (word-by-word highlight, cards)."""
from __future__ import annotations

import re

from pathlib import Path
from typing import Any

from .config import ASSETS_DIR

FONTS_DIR = ASSETS_DIR / "fonts"          # drop .ttf/.otf here to use fonts that are not installed

# Colours are CSS hex (RRGGBB). Fonts should exist on the machine (Windows names below) or in assets/fonts.
PRESETS: dict[str, dict[str, Any]] = {
    "bold_impact": {"label": "Bold (Impact, yellow highlight)", "font": "Impact", "size": 96, "primary": "#FFFFFF",
                    "highlight": "#FFE500", "outline_color": "#000000", "outline": 7, "shadow": 3,
                    "highlight_mode": "color", "uppercase": True, "position": "lower", "words_per_caption": 3, "spacing": 1},
    "clean_sans": {"label": "Clean (Bahnschrift, white, thin outline)", "font": "Bahnschrift", "size": 84, "primary": "#FFFFFF",
                   "highlight": "#7CC4FF", "outline_color": "#101418", "outline": 4, "shadow": 1,
                   "highlight_mode": "color", "uppercase": False, "position": "lower", "words_per_caption": 4, "spacing": 0},
    "boxed": {"label": "Boxed (Arial Black, word in a yellow box)", "font": "Arial Black", "size": 84, "primary": "#FFFFFF",
              "highlight": "#111111", "highlight_box": "#FFE500", "outline_color": "#000000", "outline": 5, "shadow": 0,
              "highlight_mode": "box", "uppercase": True, "position": "center", "words_per_caption": 3, "spacing": 1},
    "pop": {"label": "Pop (Impact, current word scales up, green)", "font": "Impact", "size": 92, "primary": "#FFFFFF",
            "highlight": "#3DDC84", "outline_color": "#000000", "outline": 7, "shadow": 4,
            "highlight_mode": "pop", "uppercase": True, "position": "center", "words_per_caption": 2, "spacing": 2},
    "minimal": {"label": "Minimal (Segoe UI, no highlight, bottom)", "font": "Segoe UI", "size": 72, "primary": "#FFFFFF",
                "highlight": "#FFFFFF", "outline_color": "#000000", "outline": 3, "shadow": 1,
                "highlight_mode": "none", "uppercase": False, "position": "bottom", "words_per_caption": 5, "spacing": 0},
    "neon": {"label": "Neon (Arial Black, magenta glow)", "font": "Arial Black", "size": 88, "primary": "#FFFFFF",
             "highlight": "#FF4FD8", "outline_color": "#4A0F6B", "outline": 8, "shadow": 6,
             "highlight_mode": "color", "uppercase": True, "position": "lower", "words_per_caption": 3, "spacing": 2},
}
POSITIONS = {"top": 0.22, "center": 0.50, "lower": 0.62, "bottom": 0.80}
DEFAULT_PRESET = "bold_impact"


def resolve_style(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Preset defaults with any explicit overrides from config on top."""
    cfg = dict(cfg or {})
    preset = PRESETS.get(cfg.get("style") or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET])
    style = dict(preset)
    for k, v in cfg.items():
        if k == "style" or v in (None, ""):
            continue
        style[k] = v
    style["position"] = style["position"] if style["position"] in POSITIONS else "lower"
    return style


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """CSS #RRGGBB -> ASS &HAABBGGRR."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


def _header(style: dict[str, Any], width: int, height: int) -> str:
    font = style["font"]
    size = int(style["size"])
    primary = _ass_color(style["primary"])
    highlight = _ass_color(style["highlight"])
    outline_c = _ass_color(style["outline_color"])
    box_c = _ass_color(style.get("highlight_box", style["highlight"]))
    outline, shadow, spacing = int(style["outline"]), int(style["shadow"]), int(style.get("spacing", 0))
    hi_size = int(size * (1.18 if style["highlight_mode"] == "pop" else 1.06))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},{primary},&H0000FFFF,{outline_c},&H80000000,-1,0,0,0,100,100,{spacing},0,1,{outline},{shadow},5,60,60,0,1",
        f"Style: Hi,{font},{hi_size},{highlight},&H0000FFFF,{outline_c},&H80000000,-1,0,0,0,100,100,{spacing},0,1,{outline},{shadow},5,60,60,0,1",
        # BorderStyle 3 = opaque box using BackColour; used for the "box" highlight mode
        f"Style: HiBox,{font},{size},{highlight},&H0000FFFF,{box_c},{box_c},-1,0,0,0,100,100,{spacing},0,3,{max(6, outline)},0,5,60,60,0,1",
        f"Style: Card,{font},{int(size * 1.15)},{primary},&H0000FFFF,{outline_c},&H80000000,-1,0,0,0,100,100,{spacing},0,1,{outline + 1},{shadow + 1},5,90,90,0,1",
        f"Style: CardSub,{font},{int(size * 0.7)},{highlight},&H0000FFFF,{outline_c},&H80000000,-1,0,0,0,100,100,{spacing},0,1,{max(3, outline - 1)},{shadow},5,90,90,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", "",
    ]
    return "\n".join(lines)


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


def _word(text: str, style: dict[str, Any]) -> str:
    return _esc(text.upper() if style.get("uppercase") else text)


def _highlighted(text: str, style: dict[str, Any]) -> str:
    mode = style["highlight_mode"]
    if mode == "none":
        return text
    if mode == "box":
        return "{\\rHiBox}" + text + "{\\rCap}"
    return "{\\rHi}" + text + "{\\rCap}"   # color and pop (pop uses the larger Hi size)


def write_ass(words: list[dict[str, Any]], out_path: Path, *, style: dict[str, Any] | None = None,
              full_text: str | None = None, offset: float = 0.0, width: int = 1080, height: int = 1920,
              words_per_caption: int | None = None, font: str | None = None, font_size: int | None = None) -> Path:
    """Word-highlight captions. `style` comes from resolve_style(); the keyword args keep older callers working."""
    st = resolve_style(style)
    if words_per_caption:
        st["words_per_caption"] = words_per_caption
    if font:
        st["font"] = font
    if font_size:
        st["size"] = font_size
    y = int(height * POSITIONS[st["position"]])
    events = []
    for group in group_words(words, int(st["words_per_caption"]), full_text):
        end = group[-1]["end"] + 0.05 + offset
        for k, w in enumerate(group):
            w_start = w["start"] + offset
            w_end = group[k + 1]["start"] + offset if k + 1 < len(group) else end
            parts = [(_highlighted(_word(g["text"], st), st) if j == k else _word(g["text"], st)) for j, g in enumerate(group)]
            events.append(f"Dialogue: 0,{_ts(w_start)},{_ts(w_end)},Cap,,0,0,0,,{{\\an5\\pos({width // 2},{y})}}{' '.join(parts)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_header(st, width, height) + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def write_card_ass(text: str, seconds: float, out_path: Path, *, sub_text: str = "", style: dict[str, Any] | None = None,
                   width: int = 1080, height: int = 1920, font: str | None = None, font_size: int | None = None) -> Path:
    """A title / end card: big centred text that fades in and out over `seconds`."""
    st = resolve_style(style)
    if font:
        st["font"] = font
    if font_size:
        st["size"] = font_size
    fade_ms = int(min(400, seconds * 1000 / 3))
    events = [f"Dialogue: 0,{_ts(0)},{_ts(seconds)},Card,,0,0,0,,{{\\an5\\pos({width // 2},{int(height * 0.46)})\\fad({fade_ms},{fade_ms})}}{_esc(text.upper() if st.get('uppercase') else text)}"]
    if sub_text:
        events.append(f"Dialogue: 0,{_ts(0)},{_ts(seconds)},CardSub,,0,0,0,,{{\\an5\\pos({width // 2},{int(height * 0.58)})\\fad({fade_ms},{fade_ms})}}{_esc(sub_text)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_header(st, width, height) + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def write_preview_ass(out_path: Path, style: dict[str, Any] | None = None, width: int = 1080, height: int = 1920) -> Path:
    """A still frame's worth of captions for the settings preview."""
    st = resolve_style(style)
    sample = ["most", "people", "brush", "their", "teeth", "wrong"][: int(st["words_per_caption"])]
    hi = min(1, len(sample) - 1)
    parts = [(_highlighted(_word(w, st), st) if j == hi else _word(w, st)) for j, w in enumerate(sample)]
    y = int(height * POSITIONS[st["position"]])
    events = [f"Dialogue: 0,{_ts(0)},{_ts(5)},Cap,,0,0,0,,{{\\an5\\pos({width // 2},{y})}}{' '.join(parts)}"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_header(st, width, height) + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def subtitles_filter(ass_path: Path) -> str:
    """ffmpeg subtitles filter argument, including assets/fonts when it holds font files."""
    arg = f"subtitles={ass_path.name}"
    if FONTS_DIR.exists() and any(p.suffix.lower() in (".ttf", ".otf") for p in FONTS_DIR.iterdir()):
        fonts = str(FONTS_DIR.resolve()).replace("\\", "/").replace(":", "\\:")
        arg += f":fontsdir='{fonts}'"
    return arg


def add_hook_overlay(ass_path: Path, text: str, seconds: float, *, style: dict[str, Any] | None = None,
                     width: int = 1080, height: int = 1920) -> Path:
    """Large on-screen hook from the first frame (the punchiest words of the video), above the captions, for the
    first `seconds`. Viewers decide in about a second whether to keep watching; words on screen before the voice has
    finished a sentence help them stay. Appends a style and one event to an existing captions file."""
    st = resolve_style(style)
    words = re.sub(r"\s+", " ", (text or "").strip()).upper().split()
    if not words or seconds <= 0:
        return ass_path
    # two balanced lines at most
    if len(" ".join(words)) > 16 and len(words) > 1:
        half = max(1, round(len(words) / 2))
        body = " ".join(words[:half]) + r"\N" + " ".join(words[half:])
    else:
        body = " ".join(words)
    size = int(int(st["size"]) * 1.5)
    y = int(height * (0.40 if st["position"] == "top" else 0.14))   # stay clear of the running captions
    color = _ass_color(st["highlight"])
    outline_c = _ass_color(st["outline_color"])
    style_line = (f"Style: HookTitle,{st['font']},{size},{color},&H0000FFFF,{outline_c},&H96000000,-1,0,0,0,100,100,"
                  f"{int(st.get('spacing', 0))},0,1,{max(6, int(st['outline']) + 2)},3,8,70,70,0,1")
    event = (f"Dialogue: 1,{_ts(0.0)},{_ts(seconds)},HookTitle,,0,0,0,,"
             f"{{\\an8\\pos({width // 2},{y})\\fad(0,250)}}{body}")
    content = ass_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    out, styled = [], False
    for line in lines:
        out.append(line)
        if not styled and line.startswith("Style: "):
            out.append(style_line)
            styled = True
    out.append(event)
    ass_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return ass_path