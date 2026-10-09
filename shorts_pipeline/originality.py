"""Originality guard: keep the channel from looking mass-produced.

YouTube's monetisation policy turns down channels whose videos are repetitive or "mass-produced" (the same template
with the subject swapped). This module:
  - finds title and opening formulas the channel has been overusing (e.g. "..., and 5 More ... Facts", "Your ...");
  - tells the script writer to avoid them, and lets TypeSafe judge each draft against the recent uploads
    (`template_repeat`, used by script QA: a draft that is the same template gets rewritten);
  - rotates the narrator voice among a few similar voices, so uploads do not all sound identical;
  - reports a channel "sameness" score that lowers the daily upload volume while the channel looks formulaic.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .config import load_config

# template_check (TypeSafe judging each draft against recent uploads) is off by default: on this channel it could not
# tell a distinct story told in the channel's usual format from a real formula copy (it scored them 3.3 vs 2.9 of 4).
DEFAULTS = {"enabled": True, "voices": ["en-US-AndrewNeural", "en-US-BrianNeural", "en-US-ChristopherNeural"],
            "recent": 20, "overused_share": 0.3, "template_check": False, "max_template_score": 3.5,
            "sameness_cap": 0.5, "volume_factor": 0.75}

FORMULAS = {
    "'..., and N More ...' list title": r",?\s+(and|plus)\s+\d+\s+more\b",
    "titles starting 'Your ...'": r"^your\b",
    "titles starting 'This ...'": r"^this\b",
    "titles starting 'Every Time ...'": r"^every time\b",
    "'A <person> did X' story titles": r"^(a|an)\s+(\d+-year-old|psychologist|scientist|man|woman|doctor|teacher)\b",
    "titles starting 'Why ...'": r"^why\b",
    "titles starting 'How ...'": r"^how\b",
    "'X Is/Was Actually ...' titles": r"\b(is|was|are)\s+actually\b",
    "'... in <year>' story titles": r"\bin\s+(1[0-9]{3}|20[0-2][0-9])\b",
}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("originality")) or {})
    return cfg


def recent_uploads(n: int | None = None) -> list[dict[str, str]]:
    """The channel's most recent videos (produced here or on the channel), newest first: title and opening line."""
    from .history import known_videos
    n = n or int(config()["recent"])
    known = sorted(known_videos(), key=lambda k: str(k.get("at") or ""), reverse=True)
    return [{"title": k["title"], "opening": (k.get("hook") or "")[:160]} for k in known[:n]]


def overused_formulas(recent: list[dict[str, str]] | None = None) -> list[tuple[str, float]]:
    recent = recent if recent is not None else recent_uploads()
    if not recent:
        return []
    share = []
    for name, pattern in FORMULAS.items():
        hits = sum(1 for r in recent if re.search(pattern, r["title"].strip().lower()))
        share.append((name, hits / len(recent)))
    limit = float(config()["overused_share"])
    return sorted([(n, s) for n, s in share if s >= limit], key=lambda x: -x[1])


def sameness(recent: list[dict[str, str]] | None = None) -> float:
    """0-1: the share of recent uploads that follow the single most common formula."""
    recent = recent if recent is not None else recent_uploads()
    if not recent:
        return 0.0
    return max((sum(1 for r in recent if re.search(p, r["title"].strip().lower())) / len(recent)) for p in FORMULAS.values())


def prompt_note() -> str:
    """Instruction for the script writer, empty when nothing is overused."""
    if not config()["enabled"]:
        return ""
    over = overused_formulas()
    if not over:
        return ""
    return ("\nThis channel has overused these formulas recently; do NOT use them for this video, and make its title, "
            "opening and structure clearly different from a template with the subject swapped:\n"
            + "\n".join(f"- {name} ({share:.0%} of recent uploads)" for name, share in over) + "\n")


def template_question():
    from .judge import _q
    return _q("score", (
        "Compared with `recent_uploads` (this channel's latest videos), how much does `script` look like the same template "
        "with only the subject swapped: the same title formula, the same kind of opening line, the same structure?"), [
        "Clearly distinct from every recent upload",
        "Mostly distinct; shares one minor element",
        "Noticeably formulaic: shares the title formula or the opening pattern with several recent uploads",
        "The same template as several recent uploads",
        "Practically a copy of a recent upload's template",
    ])


def voice_for(title: str, default: str) -> str:
    cfg = config()
    voices = [v for v in cfg.get("voices") or [] if v]
    if not cfg["enabled"] or not voices:
        return default
    return voices[int(hashlib.sha1(title.encode("utf-8")).hexdigest(), 16) % len(voices)]


def volume_factor() -> float:
    cfg = config()
    return float(cfg["volume_factor"]) if cfg["enabled"] and sameness() >= float(cfg["sameness_cap"]) else 1.0
