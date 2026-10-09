"""Background footage that matches what is being said.

For each script line: search Pexels for several portrait videos, ask TypeSafe which candidate actually
illustrates the sentence (Pexels video slugs describe the clip), fall back to a Pexels photo with a slow
zoom when no clip fits, and finally to a generated motion background. Without a Pexels key everything
uses the generated background; without a TypeSafe key the first search result is used.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

from .config import env, has_typesafe
from .storage import CACHE_DIR, JsonCache
from .tools import run as _run, ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()
log = logging.getLogger(__name__)

PEXELS_CACHE = CACHE_DIR / "pexels"
_SEARCH_CACHE = JsonCache("pexels_search")
_CHOICE_CACHE = JsonCache("footage_choices")
CANDIDATES = 8
MIN_FIT = 0.35        # probability the chosen clip fits; below this try photos, then the generated background


def _headers() -> dict[str, str]:
    return {"Authorization": env("PEXELS_API_KEY") or ""}


def _slug_text(url: str) -> str:
    """https://www.pexels.com/video/a-man-brushing-his-teeth-123456/ -> 'a man brushing his teeth'."""
    m = re.search(r"/(?:video|photo)/([^/]+?)-\d+/?$", url or "")
    return m.group(1).replace("-", " ") if m else ""


def search_videos(keyword: str) -> list[dict[str, Any]]:
    if not env("PEXELS_API_KEY"):
        return []
    key = f"v:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         params={"query": keyword, "orientation": "portrait", "size": "medium", "per_page": CANDIDATES},
                         headers=_headers(), timeout=20)
        r.raise_for_status()
        videos = r.json().get("videos", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("pexels video search failed for %r: %s", keyword, exc)
        return []
    out = []
    for v in videos:
        files = sorted((f for f in v.get("video_files", []) if f.get("height") and f.get("height") >= f.get("width", 0)),
                       key=lambda f: abs((f.get("height") or 0) - 1920))
        if not files:
            continue
        out.append({"id": str(v["id"]), "kind": "video", "description": _slug_text(v.get("url", "")) or keyword,
                    "duration": v.get("duration", 0), "link": files[0]["link"], "page": v.get("url", "")})
    _SEARCH_CACHE.set(key, out)
    return out


def search_photos(keyword: str) -> list[dict[str, Any]]:
    if not env("PEXELS_API_KEY"):
        return []
    key = f"p:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         params={"query": keyword, "orientation": "portrait", "per_page": CANDIDATES},
                         headers=_headers(), timeout=20)
        r.raise_for_status()
        photos = r.json().get("photos", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("pexels photo search failed for %r: %s", keyword, exc)
        return []
    out = [{"id": str(p["id"]), "kind": "photo", "description": (p.get("alt") or _slug_text(p.get("url", "")) or keyword).strip(),
            "link": (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("large"), "page": p.get("url", "")}
           for p in photos if (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("large")]
    _SEARCH_CACHE.set(key, out)
    return out


# ---------------------------------------------------------------------------------------------- Pixabay (second source)
PIXABAY_PER_SEARCH = 6


def _pixabay(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    key = env("PIXABAY_API_KEY")
    if not key:
        return []
    try:
        r = requests.get(f"https://pixabay.com/api/{path}", params={**params, "key": key, "safesearch": "true"}, timeout=20)
        r.raise_for_status()
        return r.json().get("hits", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("pixabay search failed for %r: %s", params.get("q"), str(exc)[:120])
        return []


def search_pixabay_videos(keyword: str) -> list[dict[str, Any]]:
    """Free Pixabay videos (PIXABAY_API_KEY). Mostly landscape; portrait files are preferred when offered."""
    key = f"pbv:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    out = []
    for h in _pixabay("videos/", {"q": keyword, "per_page": PIXABAY_PER_SEARCH, "video_type": "film"}):
        files = [f for f in (h.get("videos") or {}).values() if isinstance(f, dict) and f.get("url")]
        if not files:
            continue
        # portrait first, then the size closest to 1080 wide x 1920 high once cropped (height ~1080 is plenty)
        files.sort(key=lambda f: (0 if (f.get("height") or 0) >= (f.get("width") or 0) else 1, abs((f.get("height") or 0) - 1080)))
        f = files[0]
        shape = "vertical" if (f.get("height") or 0) >= (f.get("width") or 0) else "horizontal, cropped to vertical"
        out.append({"id": f"pb{h['id']}", "kind": "video", "source": "pixabay",
                    "description": f"{h.get('tags', keyword)} ({shape})", "duration": h.get("duration", 0),
                    "link": f["url"], "page": h.get("pageURL", "")})
    _SEARCH_CACHE.set(key, out)
    return out


def search_pixabay_photos(keyword: str) -> list[dict[str, Any]]:
    key = f"pbp:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    out = [{"id": f"pbp{h['id']}", "kind": "photo", "source": "pixabay", "description": h.get("tags", keyword),
            "link": h.get("largeImageURL") or h.get("webformatURL"), "page": h.get("pageURL", "")}
           for h in _pixabay("", {"q": keyword, "per_page": PIXABAY_PER_SEARCH, "image_type": "photo",
                                  "orientation": "vertical"}) if h.get("largeImageURL") or h.get("webformatURL")]
    _SEARCH_CACHE.set(key, out)
    return out


def video_candidates(keyword: str) -> list[dict[str, Any]]:
    """Pexels first (vertical files), then Pixabay; ids never collide."""
    return search_videos(keyword) + search_pixabay_videos(keyword)


def photo_candidates(keyword: str) -> list[dict[str, Any]]:
    return search_photos(keyword) + search_pixabay_photos(keyword)


def choose(line_text: str, keyword: str, candidates: list[dict[str, Any]], *, strict: bool = True,
           used: set[str] | None = None) -> tuple[dict[str, Any] | None, float]:
    """TypeSafe picks the candidate that best illustrates the line; returns (candidate, fit probability).
    strict=True wants a literal match to the sentence; strict=False accepts footage of the same subject
    (the right creature, place or object) even if it does not show the exact action."""
    if not candidates:
        return None, 0.0
    if not has_typesafe():
        return candidates[0], 0.5
    cache_key = hashlib.sha1(f"{line_text}|{keyword}|{int(strict)}|{','.join(c['id'] for c in candidates)}".encode()).hexdigest()[:20]
    cached = _CHOICE_CACHE.get(cache_key)
    if cached and "oks" in cached:
        return _pick_with_variety(candidates, cached["probs"], cached["oks"], strict, used)
    from typesafe_sdk import Choice, Noul, TypeSafeClient

    criteria = {c["id"]: c["description"] for c in candidates}
    state = {"spoken_line": line_text, "search_term": keyword,
             "candidates": [{"id": c["id"], "shows": c["description"]} for c in candidates]}
    # One request: a Choice for the single best clip, plus one Noul per candidate so several clips can
    # qualify independently (that is what lets a video vary its footage instead of repeating one clip).
    if strict:
        criteria["none"] = "None of these clips shows what the line is about"
        questions: dict[str, Any] = {
            "best": Choice(instructions={"question": "Which candidate clip best illustrates what the spoken line is about?",
                                         "note": "Prefer a literal match to the subject of the line; generic or unrelated footage is worse than none."},
                           criteria=criteria),
        }
        per = "Does the candidate clip with id `{id}` show the subject of the spoken line, so it could play while that line is spoken?"
    else:
        criteria["none"] = "None of these clips is even about the same subject"
        questions = {
            "best": Choice(instructions={"question": "Which candidate clip is about the same subject as the spoken line (the same creature, place, object or activity)?",
                                         "note": "It does not need to show the exact action described; a clip of the right subject beats an abstract background."},
                           criteria=criteria),
        }
        per = "Is the candidate clip with id `{id}` about the same subject as the spoken line (same creature, place, object or activity), even if it does not show the exact action?"
    for c in candidates:
        questions[f"ok_{c['id']}"] = Noul(instructions=per.format(id=c["id"]))
    try:
        with TypeSafeClient(timeout=60.0) as client:
            resp = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001
        log.warning("footage choice failed (%s); using first result", exc)
        return candidates[0], 0.5
    probs = {k: float(v) for k, v in resp.answers["best"].probabilities.items()}
    oks = {c["id"]: float(resp.answers[f"ok_{c['id']}"].noul) for c in candidates}
    _CHOICE_CACHE.set(cache_key, {"probs": probs, "oks": oks})
    return _pick_with_variety(candidates, probs, oks, strict, used)


def _pick_with_variety(candidates: list[dict[str, Any]], probs: dict[str, float], oks: dict[str, float],
                       strict: bool, used: set[str] | None) -> tuple[dict[str, Any] | None, float]:
    """Policy in code: any candidate whose own Noul passes qualifies; prefer an unused one (highest Noul),
    otherwise the Choice winner. 'none' winning the Choice with no qualifying Noul means nothing fits."""
    threshold = 0.6 if strict else 0.5
    used = used or set()
    qualified = sorted(((oks.get(c["id"], 0.0), c) for c in candidates if oks.get(c["id"], 0.0) >= threshold), key=lambda x: -x[0])
    fresh = [(p, c) for p, c in qualified if c["id"] not in used]
    if fresh:
        p, pick = fresh[0]
        return pick, p
    best_id = max(probs, key=probs.get) if probs else "none"
    if best_id != "none":
        pick = next((c for c in candidates if c["id"] == best_id), None)
        if pick:
            return pick, max(oks.get(best_id, 0.0), probs[best_id])
    if qualified:
        p, pick = qualified[0]
        return pick, p
    return None, 0.0


def _download(url: str, dest: Path) -> Path | None:
    if dest.exists() and dest.stat().st_size > 0:
        from .housekeeping import touch
        touch(dest)                                     # recently used: the cache pruning keeps it longer
        return dest
    PEXELS_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=90, headers=_headers()) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
        return dest
    except Exception as exc:  # noqa: BLE001
        log.warning("download failed %s: %s", url[:80], exc)
        dest.unlink(missing_ok=True)
        return None


def photo_clip(photo_url: str, photo_id: str, seconds: float, out_path: Path) -> Path | None:
    """Slow Ken Burns zoom over a portrait photo, rendered to a short mp4."""
    img = _download(photo_url, PEXELS_CACHE / f"photo_{photo_id}.jpg")
    if not img:
        return None
    return image_clip(img, seconds, out_path)


def image_clip(img: Path, seconds: float, out_path: Path) -> Path | None:
    """Slow Ken Burns zoom over any local image (stock photo or AI-generated)."""
    frames = int((seconds + 0.5) * 30)
    vf = (f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          f"zoompan=z='min(zoom+0.0006,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(img), "-vf", vf,
           "-t", f"{seconds + 0.5:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out_path)]
    try:
        _run(cmd, check=True, text=True)
    except RuntimeError as exc:
        log.warning("photo clip failed: %s", exc)
        return None
    return out_path


def generated_background(out_path: Path, seconds: float, seed: int = 7) -> Path:
    """Slow animated gradient. Always available, no key needed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    palette = [
        ("0x1b1f3b", "0x3a0f5c", "0x0b3b5c"),
        ("0x0f2027", "0x203a43", "0x2c5364"),
        ("0x3a1c71", "0xd76d77", "0xffaf7b"),
        ("0x141e30", "0x243b55", "0x0f0c29"),
    ][seed % 4]
    # No film grain here on purpose: noise defeats x264 and turns a 10 s clip into hundreds of MB.
    filt = (f"gradients=size=1080x1920:speed=0.015:nb_colors=3:c0={palette[0]}:c1={palette[1]}:c2={palette[2]}"
            f":duration={seconds:.2f}:rate=30,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", filt,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-t", f"{seconds:.2f}", str(out_path)]
    _run(cmd, check=True, text=True)
    return out_path


def footage_for_line(line_text: str, keyword: str, seconds: float, work_dir: Path, index: int,
                     fallback_keyword: str = "", used: set[str] | None = None) -> tuple[Path, str]:
    """Best matching background for one line. Returns (clip path, note for the log)."""
    # Pass 1: the line's own term, literal match. Pass 2: the video's subject term, same-subject match.
    used = used if used is not None else set()
    from . import imagegen

    ai = imagegen.settings()
    ai_on = bool(ai.get("enabled")) and ai.get("mode") != "never" and imagegen.available()
    if ai_on and ai.get("mode") == "always":
        img = imagegen.generate(line_text, keyword, fallback_keyword)
        clip = img and image_clip(img, seconds, work_dir / f"bg_{index}.mp4")
        if clip:
            return clip, f"AI image ({ai['provider']}) for '{keyword}'"
    attempts = [(keyword, True)]
    if fallback_keyword:
        attempts.append((fallback_keyword, False))
    attempts.append((keyword, False))
    reuse: tuple[dict[str, Any], float, str, bool] | None = None    # a clip already in this Short: last resort only
    for kw, strict in attempts:
        for kind, cands in (("video", video_candidates(kw)), ("photo", photo_candidates(kw))):
            pick, fit = choose(line_text, kw, cands, strict=strict, used=used)
            if not pick or fit < MIN_FIT:
                continue
            if pick["id"] in used:
                reuse = reuse or (pick, fit, kw, strict)
                continue
            clip = _materialize(pick, kind, seconds, work_dir, index)
            if clip:
                used.add(pick["id"])
                src = " on Pixabay" if pick.get("source") == "pixabay" else ""
                return clip, (f"{kind} '{pick['description'][:60]}'{src} ({fit:.0%} "
                              f"{'literal' if strict else 'same-subject'} fit, '{kw}')")
    if reuse and not ai_on:
        pick, fit, kw, strict = reuse
        clip = _materialize(pick, pick.get("kind", "video"), seconds, work_dir, index)
        if clip:
            return clip, f"{pick.get('kind', 'video')} '{pick['description'][:60]}' again (nothing new fit '{kw}')"
    if ai_on:
        img = imagegen.generate(line_text, keyword, fallback_keyword)
        clip = img and image_clip(img, seconds, work_dir / f"bg_{index}.mp4")
        if clip:
            return clip, f"AI image ({ai['provider']}) for '{keyword}' (nothing on Pexels matched)"
    clip = generated_background(work_dir / f"bg_{index}.mp4", seconds + 0.5, seed=index)
    return clip, f"generated background (nothing on Pexels matched '{keyword}' or '{fallback_keyword}')"


def _materialize(pick: dict[str, Any], kind: str, seconds: float, work_dir: Path, index: int) -> Path | None:
    if kind == "photo":
        return photo_clip(pick["link"], pick["id"], seconds, work_dir / f"bg_{index}.mp4")
    return _download(pick["link"], PEXELS_CACHE / f"{pick['id']}.mp4")


def plan_backgrounds(script: dict[str, Any], words: list[dict[str, Any]], total_seconds: float,
                     source: str, work_dir: Path) -> list[dict[str, Any]]:
    """One background segment per script line (or one generated clip for the whole video)."""
    from . import imagegen

    use_pexels = source in ("auto", "pexels") and bool(env("PEXELS_API_KEY"))
    ai = imagegen.settings()
    use_ai = source != "generated" and bool(ai.get("enabled")) and ai.get("mode") != "never" and imagegen.available()
    if not use_pexels and not use_ai:
        clip = generated_background(work_dir / "bg.mp4", total_seconds + 0.5)
        return [{"path": str(clip), "start": 0.0, "end": total_seconds}]
    if not use_pexels:
        # no Pexels key: every line gets an AI image (the search steps are skipped inside footage_for_line)
        _SEARCH_CACHE.set("v:__nopexels__", [])

    chunks = [script["hook"], *[ln["text"] for ln in script["lines"]], script["cta"]]
    first_kw = script["lines"][0]["visual_keyword"] if script["lines"] else script.get("title", "abstract")
    keywords = [first_kw, *[ln["visual_keyword"] for ln in script["lines"]], keywords_fallback(script)]
    fallback = keywords_fallback(script)
    segments: list[dict[str, Any]] = []
    used: set[str] = set()
    idx, t = 0, 0.0
    for i, (chunk, kw) in enumerate(zip(chunks, keywords)):
        n = len(chunk.split())
        group = words[idx: idx + n]
        idx += n
        end = total_seconds if i == len(chunks) - 1 else (group[-1]["end"] if group else t)
        if not chunk.strip() or end <= t:
            continue
        clip, note = footage_for_line(chunk, kw, end - t, work_dir, i, fallback_keyword=fallback if kw != fallback else "", used=used)
        log.info("footage %d/%d: %s", i + 1, len(chunks), note)
        segments.append({"path": str(clip), "start": t, "end": end})
        t = end
    return segments


def keywords_fallback(script: dict[str, Any]) -> str:
    """A subject-level search term used when a line's own term finds nothing that fits."""
    kw = script.get("visual_fallback") or ""
    if kw:
        return kw
    title = re.sub(r"[^\w ]", "", script.get("title", "")).strip()
    return title or "abstract background"
