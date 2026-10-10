"""Dubbed versions of the main channel's best Shorts for a second-language channel.

Runs inside the second channel's workspace (`python main.py channels add es`, then `schedule.source: dub` in that
channel's Settings). Each scheduled slot takes the main channel's best-performing public Short that has not been
dubbed yet, has the script writer translate it line by line into natural spoken language (same facts, same order,
same English footage search terms so the footage fits as well), and produces it with a native voice. Captions, the
on-screen hook, title, description and hashtags come out in that language; the channel's own queue, publish times
and feedback do the rest.
"""
from __future__ import annotations

import logging
import statistics as st
import time
from typing import Any

from .config import DATA_DIR, ROOT, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "dubs.json"
LANGUAGES = {"es": ("Latin American Spanish", "es-MX-JorgeNeural"), "pt": ("Brazilian Portuguese", "pt-BR-AntonioNeural"),
             "fr": ("French", "fr-FR-HenriNeural"), "de": ("German", "de-DE-ConradNeural"), "hi": ("Hindi", "hi-IN-MadhurNeural")}
DEFAULTS = {"language": "es", "voice": "", "source_channel": "main", "min_lift": 1.0}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(load_config().get("dub") or {})
    lang = LANGUAGES.get(cfg["language"], LANGUAGES["es"])
    cfg["language_name"] = lang[0]
    cfg["voice"] = cfg["voice"] or lang[1]
    return cfg


def _source_paths() -> tuple[Any, Any]:
    src = config()["source_channel"]
    home = ROOT if src in ("", "main") else ROOT / "channels" / src
    return home / "output", home / "data" / "channel_stats.json"


def candidates() -> list[dict[str, Any]]:
    """The source channel's public Shorts with a script, best views per hour first, not dubbed yet."""
    out_dir, stats_path = _source_paths()
    stats = {v["id"]: v for v in (load_json(stats_path) or {}).get("videos", [])}
    done = (load_json(STATE_PATH) or {}).get("dubbed", {})
    rows = []
    if not out_dir.exists():
        return rows
    vphs = [float(v.get("views_per_hour") or 0) for v in stats.values() if v.get("privacy", "public") == "public"]
    med = st.median(vphs) if vphs else 0.0
    for d in out_dir.iterdir():
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if not meta or not meta.get("youtube_id") or d.name in done or not (d / "script.json").exists():
            continue
        s = stats.get(meta["youtube_id"])
        if not s or s.get("privacy", "public") != "public":
            continue
        if float(s.get("views_per_hour") or 0) < float(config()["min_lift"]) * med:
            continue
        rows.append({"dir": d.name, "path": d, "title": meta["title"], "vph": float(s.get("views_per_hour") or 0),
                     "youtube_id": meta["youtube_id"]})
    return sorted(rows, key=lambda r: -r["vph"])


def next_source() -> str | None:
    c = candidates()
    return c[0]["dir"] if c else None


def translate(script: dict[str, Any]) -> dict[str, Any]:
    """The script in the target language, line by line, footage terms unchanged."""
    from . import claude_code_backend, script_gen
    cfg = config()
    lines = script.get("lines") or []
    system = (f"You translate YouTube Shorts voiceover scripts into natural, spoken {cfg['language_name']} for a native "
              "narrator. Keep every fact, number and name, the order of lines and their count, and the punchy rhythm. "
              "Translate meaning, not word for word. Most languages take more words than English: tighten the wording "
              "so the voiceover is no longer than the original. Output only the JSON object.")
    prompt = (f"Translate this Short. Return exactly {len(lines)} lines in the same order. The title must be at most 70 "
              "characters; hashtags without '#', in the target language, first one 'shorts'.\n\n"
              + __import__("json").dumps({k: script.get(k) for k in ("title", "hook", "cta", "description", "hashtags",
                                                                      "thumbnail_text", "comment_question")}
                                         | {"lines": [ln["text"] for ln in lines]}, ensure_ascii=False))
    schema = {"type": "object", "properties": {
        "title": {"type": "string"}, "hook": {"type": "string"}, "lines": {"type": "array", "items": {"type": "string"}},
        "cta": {"type": "string"}, "description": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
        "thumbnail_text": {"type": "string"}, "comment_question": {"type": "string"}},
        "required": ["title", "hook", "lines", "cta", "description", "hashtags", "thumbnail_text"]}
    backend = script_gen.pick_backend(load_config()["production"].get("script_backend", "auto"))
    if backend == "ollama":
        from . import ollama_backend
        data = ollama_backend.generate_json(system, prompt, schema)
    else:
        data = claude_code_backend.generate_json(system, prompt, schema)
    tr_lines = data.get("lines") or []
    if len(tr_lines) != len(lines):
        raise RuntimeError(f"the translation has {len(tr_lines)} lines, the original {len(lines)}")
    out = dict(script)
    out.update({k: data.get(k, script.get(k)) for k in ("title", "hook", "cta", "description", "hashtags",
                                                        "thumbnail_text", "comment_question")})
    out["lines"] = [{"text": t, "visual_keyword": ln["visual_keyword"]} for t, ln in zip(tr_lines, lines)]
    out["full_text"] = " ".join([out["hook"], *tr_lines, out["cta"]])
    out["word_count"] = len(out["full_text"].split())
    out["language"] = config()["language"]
    out["backend"] = "dub"
    return script_gen.finalize_metadata(out)


def record(source_dir: str, out_dir: str) -> None:
    state = load_json(STATE_PATH) or {"dubbed": {}}
    state["dubbed"][source_dir] = {"out_dir": out_dir, "at": time.time()}
    save_json(STATE_PATH, state)
