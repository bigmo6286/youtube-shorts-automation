"""The video frame of the production in progress: vertical 1080x1920 for Shorts, landscape 1920x1080 for long videos.

Set once at the start of every production (`use`); footage searches, crops, AI images, captions, the render, the
thumbnail and the Telegram preview read it, so a long video is landscape end to end. Jobs run one at a time.
"""
from __future__ import annotations

SIZES = {"vertical": (1080, 1920), "landscape": (1920, 1080)}
_state = {"orientation": "vertical", "long": False}


def use(orientation: str = "vertical", long: bool = False) -> None:
    _state["orientation"] = orientation if orientation in SIZES else "vertical"
    _state["long"] = bool(long)


def size() -> tuple[int, int]:
    return SIZES[_state["orientation"]]


def landscape() -> bool:
    return _state["orientation"] == "landscape"


def long_video() -> bool:
    return _state["long"]
