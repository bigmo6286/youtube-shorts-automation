"""Weekly compilations: the week's best Shorts on one topic, stitched into one landscape video with chapters.

Once a week (schedule.compilations: weekday and hour) the engine looks at the Shorts uploaded in the last window_days,
groups them by series or topic, takes the group with the most Shorts and keeps its best ones by views per hour (what the
channel sync measured). Each part plays in the middle of a 1920x1080 frame over a blurred, darkened copy of itself,
with a short title card before it; the description carries YouTube chapters (one per part), a link to every original
Short, and the music and photo credits of the parts. A compilation is a long video: it is uploaded (private, as every
upload here) when `upload` is on, and never joins the Shorts queue. The cleanup keeps the video file of recent,
better-than-median Shorts so there is something to stitch (see housekeeping).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from pydantic import BaseModel, Field

from .config import DATA_DIR, OUTPUT_DIR, load_config
from .storage import load_json, now_iso, save_json

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "weekday": 6, "hour": 11, "window_days": 7, "min_parts": 4, "max_parts": 8,
            "card_seconds": 1.6, "upload": True}
STATE_PATH = DATA_DIR / "compilations.json"
W, H = 1920, 1080
KEEP_FACTOR = 0.8               # a recent Short keeps its video file while its views/hour is at least this x the week's median


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("schedule") or {}).get("compilations")) or {})
    return cfg


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")[:40] or "mix"


def group_of(meta: dict[str, Any]) -> tuple[str, str]:
    """(group key, label): the series when the Short is one, else the engine's topic label."""
    bp = meta.get("blueprint") or {}
    if bp.get("series"):
        return f"series:{_slug(bp['series'])}", str(bp["series"]).strip()
    topic = str(bp.get("topic") or "other")
    return f"topic:{topic}", topic.replace("_", " ")


def _uploaded_at(meta: dict[str, Any], d: Path) -> float:
    if meta.get("uploaded_at"):
        return float(meta["uploaded_at"])
    try:
        return time.mktime(time.strptime(d.name[:19], "%Y-%m-%dT%H-%M-%S")) - time.timezone
    except ValueError:
        return d.stat().st_mtime


def _is_short(meta: dict[str, Any]) -> bool:
    return bool(meta.get("youtube_id")) and meta.get("privacy") != "deleted" and not meta.get("long_video") \
        and meta.get("mode") != "compilation"


def candidates(now: float | None = None, window_days: float | None = None, output_dir: Path | None = None,
               need_video: bool = True) -> list[dict[str, Any]]:
    """Uploaded Shorts of the last window, not yet in a compilation, with what is known about how they did."""
    now = now or time.time()
    cfg = config()
    window = float(window_days or cfg["window_days"])
    output_dir = output_dir or OUTPUT_DIR
    out: list[dict[str, Any]] = []
    if not output_dir.exists():
        return out
    for d in output_dir.iterdir():
        if not d.is_dir():
            continue
        meta = load_json(d / "meta.json")
        if not meta or not _is_short(meta) or meta.get("compiled_in"):
            continue
        up = _uploaded_at(meta, d)
        if now - up > window * 86400:
            continue
        if need_video and not (d / "short.mp4").exists():
            continue
        stats = meta.get("channel_stats") or {}
        key, label = group_of(meta)
        out.append({"dir": d.name, "path": d / "short.mp4", "title": meta.get("title", ""), "youtube_id": meta["youtube_id"],
                    "views": int(stats.get("views") or 0), "vph": float(stats.get("views_per_hour") or 0.0),
                    "priority": int(meta.get("upload_priority") or 0), "duration": float(meta.get("duration") or 0),
                    "group": key, "label": label, "uploaded_at": up, "music": meta.get("music"),
                    "photo_credits": meta.get("photo_credits") or [], "hashtags": meta.get("hashtags") or [],
                    "privacy": meta.get("privacy")})
    return out


def pick(cands: list[dict[str, Any]], cfg: dict[str, Any] | None = None, topic: str | None = None) -> dict[str, Any] | None:
    """The group with the most Shorts (ties: more views/hour), its best `max_parts` first; None when no group has
    `min_parts`. `topic` restricts to one series or topic label."""
    cfg = cfg or config()
    if topic:
        t = _slug(topic)
        cands = [c for c in cands if _slug(c["label"]) == t or c["group"].endswith(":" + t)]
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in cands:
        groups.setdefault(c["group"], []).append(c)
    eligible = [g for g in groups.values() if len(g) >= int(cfg["min_parts"])]
    if not eligible:
        return None
    best = max(eligible, key=lambda g: (len(g), sum(c["vph"] for c in g)))
    ranked = sorted(best, key=lambda c: (-c["vph"], -c["views"], -c["priority"], -c["uploaded_at"]))[: int(cfg["max_parts"])]
    return {"group": best[0]["group"], "label": best[0]["label"], "parts": ranked}


# ---------------------------------------------------------------------------------------------------- keeping files
_MEDIAN_CACHE: dict[str, Any] = {"at": 0.0, "value": None, "dir": None}


def _window_median_vph(now: float, output_dir: Path | None) -> float | None:
    key = str(output_dir or OUTPUT_DIR)
    if now - _MEDIAN_CACHE["at"] < 600 and _MEDIAN_CACHE["dir"] == key:
        return _MEDIAN_CACHE["value"]
    rates = [c["vph"] for c in candidates(now=now, output_dir=output_dir, need_video=False) if c["vph"] > 0]
    value = median(rates) if len(rates) >= 4 else None
    _MEDIAN_CACHE.update(at=now, value=value, dir=key)
    return value


def wants_video(meta: dict[str, Any], d: Path, now: float | None = None, output_dir: Path | None = None) -> bool:
    """Should the cleanup keep this uploaded Short's video file for a coming compilation? Yes while it is inside the
    window, not compiled yet, and doing at least KEEP_FACTOR x the week's median views/hour (unknown counts as yes)."""
    cfg = config()
    if not cfg["enabled"] or not _is_short(meta) or meta.get("compiled_in"):
        return False
    now = now or time.time()
    if now - _uploaded_at(meta, d) > (float(cfg["window_days"]) + 1) * 86400:
        return False
    vph = float((meta.get("channel_stats") or {}).get("views_per_hour") or 0.0)
    if vph <= 0:
        return True
    med = _window_median_vph(now, output_dir)
    return med is None or vph >= KEEP_FACTOR * med


# ---------------------------------------------------------------------------------------------------- the video
def build(parts: list[dict[str, Any]], out_dir: Path, *, card_seconds: float, style: dict[str, Any] | None,
          out_name: str = "short.mp4") -> tuple[Path, list[tuple[float, str]], float]:
    """Stitch the parts: title card, then the Short centred over a blurred copy of itself, for each. Returns the video,
    the chapter marks (start, title) and the total length."""
    from .captions import subtitles_filter, write_card_ass
    from .render import ENCODERS, _forget_encoder, probe_duration, probe_encoder
    from .tools import run as _run

    out_dir.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    chapters: list[tuple[float, str]] = []
    t = 0.0
    idx = 0
    n = len(parts)
    for i, p in enumerate(parts):
        length = probe_duration(Path(p["path"]))
        ass = write_card_ass(p["title"], card_seconds, out_dir / f"card{i}.ass", sub_text=f"Part {i + 1} of {n}",
                             style=style, width=W, height=H, font_size=max(48, int((style or {}).get("size") or 84) * 0.8))
        inputs += ["-f", "lavfi", "-i", f"color=c=0x101418:s={W}x{H}:d={card_seconds:.2f}:r=30"]
        filters.append(f"[{idx}:v]{subtitles_filter(ass)},format=yuv420p,setsar=1[c{i}]")
        idx += 1
        inputs += ["-f", "lavfi", "-t", f"{card_seconds:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        filters.append(f"[{idx}:a]asetpts=PTS-STARTPTS[ca{i}]")
        idx += 1
        inputs += ["-i", str(Path(p["path"]).resolve())]
        filters.append(f"[{idx}:v]split[bg{i}][fg{i}];"
                       f"[bg{i}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:4,eq=brightness=-0.25[bgb{i}];"
                       f"[fg{i}]scale=-2:{H}[fgs{i}];[bgb{i}][fgs{i}]overlay=(W-w)/2:0,fps=30,format=yuv420p,setsar=1[p{i}]")
        filters.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[pa{i}]")
        idx += 1
        labels += [f"[c{i}][ca{i}]", f"[p{i}][pa{i}]"]
        chapters.append((t, p["title"]))
        t += card_seconds + length
    filters.append("".join(labels) + f"concat=n={2 * n}:v=1:a=1[vout][aout]")
    out_path = out_dir / out_name
    encoder = probe_encoder()

    def command(enc: str) -> list[str]:
        return ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters),
                "-map", "[vout]", "-map", "[aout]", *ENCODERS[enc], "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path.name]
    try:
        _run(command(encoder), check=True, cwd=str(out_dir.resolve()), text=True)
    except RuntimeError:
        if encoder == "libx264":
            raise
        log.warning("hardware encoder %s failed on the compilation; using libx264", encoder)
        _forget_encoder()
        _run(command("libx264"), check=True, cwd=str(out_dir.resolve()), text=True)
    return out_path.resolve(), chapters, t


# ---------------------------------------------------------------------------------------------------- words
class _Words(BaseModel):
    title: str = Field(description="A YouTube title for the compilation, under 70 characters, specific and curious, no clickbait")
    intro: str = Field(description="One or two sentences for the top of the description: what the viewer gets")


def words_for(label: str, parts: list[dict[str, Any]], backend: str = "auto") -> tuple[str, str]:
    """Title and description opener, from Claude when it is available, else a plain template."""
    title = f"The best {label} stories of the week ({len(parts)} in one video)"
    intro = f"{len(parts)} of our most watched {label} Shorts this week, back to back."
    try:
        from . import script_gen
        chosen = script_gen.pick_backend(backend)
        if chosen == "ollama":
            return title[:100], intro
        import anthropic
        client = anthropic.Anthropic() if chosen == "api" else None
        prompt = (f"A compilation video of {len(parts)} short narrated videos about {label}. Their titles, best first:\n"
                  + "\n".join(f"- {p['title']}" for p in parts) + "\nWrite the compilation's title and description opener.")
        out = script_gen._draft(client, "You title YouTube compilation videos for a faceless educational channel.", prompt,
                                chosen, model_cls=_Words)
        if out.title.strip():
            title = out.title.strip()
        if out.intro.strip():
            intro = out.intro.strip()
    except Exception as exc:  # noqa: BLE001
        log.info("compilation words: using the template (%s)", str(exc)[:120])
    return title[:100], intro


def description_for(intro: str, chapters: list[tuple[float, str]], parts: list[dict[str, Any]]) -> str:
    from .music import attribution_line
    lines = [intro, "", "Chapters:"]
    for t, title in chapters:
        lines.append(f"{int(t // 60)}:{int(t % 60):02d} {title}")
    lines += ["", "Watch each one on its own:"]
    for p in parts:
        lines.append(f"- {p['title']}: https://youtube.com/shorts/{p['youtube_id']}")
    credits: list[str] = []
    for p in parts:
        if p.get("music"):
            try:
                c = attribution_line(p["music"])
                if c and c not in credits:
                    credits.append(c)
            except Exception:  # noqa: BLE001
                pass
    if credits:
        lines += ["", *credits]
    photos = [c for p in parts for c in (p.get("photo_credits") or [])]
    if photos:
        try:
            from .commons import credit_line
            lines += ["", credit_line(photos)]
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines)[:4900]


def hashtags_for(parts: list[dict[str, Any]], label: str) -> list[str]:
    counts: dict[str, int] = {}
    for p in parts:
        for h in p.get("hashtags") or []:
            h = h.lstrip("#").lower()
            if h and h != "shorts":
                counts[h] = counts.get(h, 0) + 1
    tags = [h for h, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:6]
    for extra in (_slug(label).replace("_", ""), "compilation"):
        if extra and extra not in tags:
            tags.append(extra)
    return tags[:8]


# ---------------------------------------------------------------------------------------------------- the run
def run(*, topic: str | None = None, days: float | None = None, upload: bool | None = None,
        now: float | None = None, telegram: bool = True) -> Path:
    """Make this week's compilation. Raises RuntimeError with a plain reason when there is nothing to compile."""
    from . import frame, history
    from .cli import _caption_style, _notify_telegram, _upload

    cfg = config()
    now = now or time.time()
    cands = candidates(now=now, window_days=days)
    choice = pick(cands, cfg, topic)
    if not choice:
        have = len(cands)
        groups = {}
        for c in cands:
            groups[c["label"]] = groups.get(c["label"], 0) + 1
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(groups.items(), key=lambda kv: -kv[1])[:5]) or "none"
        raise RuntimeError(f"Nothing to compile: {have} uploaded Short{'s' if have != 1 else ''} with a video file in the last "
                           f"{days or cfg['window_days']} days (by topic: {detail}); a compilation needs {cfg['min_parts']} on one topic"
                           + (f" matching {topic!r}" if topic else "") + ".")
    parts, label = choice["parts"], choice["label"]
    log.info("compilation: %d %s Shorts (%s)", len(parts), label, "; ".join(p["title"][:40] for p in parts))
    frame.use("landscape", long=True)
    pcfg = load_config()["production"]
    out_dir = OUTPUT_DIR / f"{now_iso()}_compilation_{_slug(label)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    video, chapters, total = build(parts, out_dir, card_seconds=float(cfg["card_seconds"]), style=_caption_style(pcfg))
    log.info("compilation rendered: %.0f s, %d chapters", total, len(chapters))
    title, intro = words_for(label, parts, pcfg.get("script_backend", "auto"))
    description = description_for(intro, chapters, parts)
    hashtags = hashtags_for(parts, label)
    thumb = None
    try:
        from .thumbnail import make_thumbnail
        thumb = make_thumbnail({"title": title, "thumbnail_text": f"{len(parts)} {label} stories"}, [], out_dir,
                               style=_caption_style(pcfg), source=video, seek_at=float(cfg["card_seconds"]) + 0.8)
    except Exception as exc:  # noqa: BLE001
        log.warning("compilation thumbnail failed: %s", str(exc)[:120])
    meta = {"run": "compilation", "blueprint": {"format": "compilation", "topic": choice["group"].split(":", 1)[1],
                                                "hook_style": "card", "series": label if choice["group"].startswith("series:") else None},
            "video": str(video), "title": title, "description": description, "hashtags": hashtags, "duration": total,
            "music": None, "intro": False, "outro": False, "thumbnail": str(thumb) if thumb else None,
            "segments": [], "photo_credits": [], "mode": "compilation", "long_video": True, "orientation": "landscape",
            "parts": [p["dir"] for p in parts], "chapters": [[round(t, 2), c] for t, c in chapters],
            "thumbnail_text": f"{len(parts)} {label} stories"}
    save_json(out_dir / "meta.json", meta)
    save_json(out_dir / "script.json", {"title": title, "full_text": intro, "backend": "compilation", "hashtags": hashtags,
                                        "lines": [{"text": c} for _, c in chapters]})
    for p in parts:
        m = load_json(OUTPUT_DIR / p["dir"] / "meta.json")
        if m:
            m["compiled_in"] = out_dir.name
            save_json(OUTPUT_DIR / p["dir"] / "meta.json", m)
    history.record(title, key="compilation", out_dir=out_dir, source="compilation")
    st = load_json(STATE_PATH) or {}
    st["last_run"] = {"at": now, "dir": out_dir.name, "label": label, "parts": len(parts)}
    save_json(STATE_PATH, st)
    print(f"\nTITLE: {title}\n\n{description}\n\nRendered {video}  ({total:.0f}s)")
    note = f"Weekly compilation: {len(parts)} {label} Shorts, {int(total // 60)} min {int(total % 60)} s, {len(chapters)} chapters."
    do_upload = cfg["upload"] if upload is None else upload
    if do_upload:
        try:
            _upload(out_dir, interactive=False)
            meta = load_json(out_dir / "meta.json") or meta
        except Exception as exc:  # noqa: BLE001
            log.error("compilation upload failed: %s", str(exc)[:200])
            note += f"\nUpload failed ({str(exc)[:120]}); use Upload on its card."
    else:
        note += "\nNot uploaded: use Upload on its card or /upload on Telegram."
    if telegram:
        _notify_telegram(out_dir, meta, extra=note)
    return out_dir


def due(now: datetime | None = None) -> bool:
    """Is this week's compilation due (right weekday, past the hour, not yet submitted this week)?"""
    cfg = config()
    if not cfg["enabled"]:
        return False
    now = now or datetime.now()
    if now.weekday() != int(cfg["weekday"]) or now.hour < int(cfg["hour"]):
        return False
    st = load_json(STATE_PATH) or {}
    week = now.strftime("%G-W%V")
    return st.get("last_week") != week


def maybe_weekly(now: datetime, submit, ready=None) -> dict[str, Any] | None:
    """Scheduler hook: submit the compilation job when it is due and there is something to compile (checked at most
    once an hour, so a quiet week costs nothing). Marks the week only when the job was accepted."""
    if not due(now):
        return None
    st = load_json(STATE_PATH) or {}
    if time.time() - float(st.get("last_check") or 0) < 3600:
        return None
    try:
        ok = bool(ready()) if ready else pick(candidates()) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("weekly compilation check failed: %s", str(exc)[:160])
        ok = False
    if not ok:
        st["last_check"] = time.time()
        save_json(STATE_PATH, st)
        log.info("weekly compilation: nothing to compile yet (fewer than %d uploaded Shorts with a video file on one topic this week)",
                 int(config()["min_parts"]))
        return None
    job = submit("compile", {"scheduled": True})
    if job is None:
        return None                                     # the machine is busy; try again on the next tick
    st = load_json(STATE_PATH) or {}
    st["last_week"] = now.strftime("%G-W%V")
    st["last_submitted"] = {"at": time.time(), "job": job.get("id")}
    save_json(STATE_PATH, st)
    log.info("weekly compilation submitted -> job %s", job.get("id"))
    return job
