"""Background footage that matches what is being said.

For each script line: when the line is about one specific named thing (a landmark, a famous person, a ship), a real
photo of it from Wikimedia Commons comes first (see commons.py). Otherwise search Pexels for several portrait
videos, ask TypeSafe which candidate actually illustrates the sentence (Pexels video slugs describe the clip), fall
back to a Pexels photo with a slow zoom when no clip fits, and finally to a generated motion background. Without a Pexels key everything
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


def _orient_is_landscape() -> bool:
    from .frame import landscape
    return landscape()


def _orient() -> str:
    from .frame import landscape
    return "landscape" if landscape() else "portrait"


def search_videos(keyword: str) -> list[dict[str, Any]]:
    if not env("PEXELS_API_KEY"):
        return []
    key = f"v:{keyword.lower()}" if _orient() == "portrait" else f"vL:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         params={"query": keyword, "orientation": _orient(), "size": "medium", "per_page": CANDIDATES},
                         headers=_headers(), timeout=20)
        r.raise_for_status()
        videos = r.json().get("videos", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("pexels video search failed for %r: %s", keyword, exc)
        return []
    out = []
    for v in videos:
        if _orient() == "portrait":
            files = sorted((f for f in v.get("video_files", []) if f.get("height") and f.get("height") >= f.get("width", 0)),
                           key=lambda f: abs((f.get("height") or 0) - 1920))
        else:
            files = sorted((f for f in v.get("video_files", []) if f.get("width") and f.get("width") >= f.get("height", 0)),
                           key=lambda f: abs((f.get("width") or 0) - 1920))
        if not files:
            continue
        out.append({"id": str(v["id"]), "kind": "video", "description": _slug_text(v.get("url", "")) or keyword,
                    "duration": v.get("duration", 0), "link": files[0]["link"], "page": v.get("url", ""),
                    "preview": v.get("image") or ""})
    _SEARCH_CACHE.set(key, out)
    return out


def search_photos(keyword: str) -> list[dict[str, Any]]:
    if not env("PEXELS_API_KEY"):
        return []
    key = f"p:{keyword.lower()}" if _orient() == "portrait" else f"pL:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         params={"query": keyword, "orientation": _orient(), "per_page": CANDIDATES},
                         headers=_headers(), timeout=20)
        r.raise_for_status()
        photos = r.json().get("photos", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("pexels photo search failed for %r: %s", keyword, exc)
        return []
    out = [{"id": str(p["id"]), "kind": "photo", "description": (p.get("alt") or _slug_text(p.get("url", "")) or keyword).strip(),
            "link": (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("large"), "page": p.get("url", ""),
            "preview": (p.get("src") or {}).get("large") or ""}
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
    key = f"pbv:{keyword.lower()}" if _orient() == "portrait" else f"pbvL:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    out = []
    for h in _pixabay("videos/", {"q": keyword, "per_page": PIXABAY_PER_SEARCH, "video_type": "film"}):
        files = [f for f in (h.get("videos") or {}).values() if isinstance(f, dict) and f.get("url")]
        if not files:
            continue
        # portrait first, then the size closest to 1080 wide x 1920 high once cropped (height ~1080 is plenty)
        tall = _orient() == "portrait"
        files.sort(key=lambda f: (0 if ((f.get("height") or 0) >= (f.get("width") or 0)) == tall else 1,
                                  abs((f.get("height") or 0) - 1080)))
        f = files[0]
        is_tall = (f.get("height") or 0) >= (f.get("width") or 0)
        shape = ("vertical" if is_tall else "horizontal") + ("" if is_tall == tall else
                                                              f", cropped to {'vertical' if tall else 'horizontal'}")
        out.append({"id": f"pb{h['id']}", "kind": "video", "source": "pixabay",
                    "description": f"{h.get('tags', keyword)} ({shape})", "duration": h.get("duration", 0),
                    "link": f["url"], "page": h.get("pageURL", ""), "preview": f.get("thumbnail") or ""})
    _SEARCH_CACHE.set(key, out)
    return out


def search_pixabay_photos(keyword: str) -> list[dict[str, Any]]:
    key = f"pbp:{keyword.lower()}" if _orient() == "portrait" else f"pbpL:{keyword.lower()}"
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        return cached
    out = [{"id": f"pbp{h['id']}", "kind": "photo", "source": "pixabay", "description": h.get("tags", keyword),
            "link": h.get("largeImageURL") or h.get("webformatURL"), "page": h.get("pageURL", ""),
            "preview": h.get("webformatURL") or ""}
           for h in _pixabay("", {"q": keyword, "per_page": PIXABAY_PER_SEARCH, "image_type": "photo",
                                  "orientation": "vertical" if _orient() == "portrait" else "horizontal"})
           if h.get("largeImageURL") or h.get("webformatURL")]
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
    if cached and "oks" in cached and "conflicts" in cached:
        return _pick_with_variety(candidates, cached["probs"], cached["oks"], strict, used,
                                  cached["needs_setting"], cached["settings"], cached["conflicts"])
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
        # The setting check: "a volcano" is the right subject for a line about Antarctica's Mount Erebus, but a
        # volcano with green forest on it contradicts the line (viewers noticed and said so).
        questions[f"setting_{c['id']}"] = Noul(instructions=(
            f"Does the candidate clip with id `{c['id']}`, as described in `candidates`, visibly show the environment "
            "the spoken line is set in (for example polar ice or snow, desert, underwater, outer space, night)? Answer "
            "no if its description shows a different environment or gives no sign of that one."))
        questions[f"conflict_{c['id']}"] = Noul(instructions=(
            f"Does the candidate clip with id `{c['id']}`, as described in `candidates`, name or show a different "
            "specific place, landmark, country or environment than the spoken line (for example 'mount etna' or "
            "'colima volcano in mexico' for a line about Krakatoa or Antarctica)? A generic clip that names no place "
            "is not a conflict."))
    questions["needs_setting"] = Noul(instructions=(
        "Is `spoken_line` set in a specific environment that must be visible on screen for the footage not to "
        "contradict it: polar ice or snow, desert, underwater, outer space, night, jungle, or the like? A named place "
        "or a date alone does not count."))
    try:
        with TypeSafeClient(timeout=60.0) as client:
            resp = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001
        log.warning("footage choice failed (%s); using first result", exc)
        return candidates[0], 0.5
    probs = {k: float(v) for k, v in resp.answers["best"].probabilities.items()}
    oks = {c["id"]: float(resp.answers[f"ok_{c['id']}"].noul) for c in candidates}
    settings = {c["id"]: float(resp.answers[f"setting_{c['id']}"].noul) for c in candidates}
    conflicts = {c["id"]: float(resp.answers[f"conflict_{c['id']}"].noul) for c in candidates}
    needs_setting = float(resp.answers["needs_setting"].noul)
    _CHOICE_CACHE.set(cache_key, {"probs": probs, "oks": oks, "settings": settings, "conflicts": conflicts,
                                  "needs_setting": needs_setting})
    return _pick_with_variety(candidates, probs, oks, strict, used, needs_setting, settings, conflicts)


SETTING_NEEDED = 0.6      # the line depends on a setting (place, climate, era) when its Noul is at least this
SETTING_MATCH = 0.5       # a clip must then match that setting at least this much
CONFLICT = 0.5            # a clip that names or shows a different place than the line is never used


def _pick_with_variety(candidates: list[dict[str, Any]], probs: dict[str, float], oks: dict[str, float],
                       strict: bool, used: set[str] | None, needs_setting: float = 0.0,
                       settings: dict[str, float] | None = None,
                       conflicts: dict[str, float] | None = None) -> tuple[dict[str, Any] | None, float]:
    """Policy in code: any candidate whose own Noul passes qualifies; prefer an unused one (highest Noul),
    otherwise the Choice winner. 'none' winning the Choice with no qualifying Noul means nothing fits.
    When the line depends on a setting, a clip that does not match it never qualifies."""
    threshold = 0.6 if strict else 0.5
    used = used or set()
    if conflicts:
        candidates = [c for c in candidates if conflicts.get(c["id"], 0.0) < CONFLICT]
    if needs_setting >= SETTING_NEEDED and settings is not None:
        candidates = [c for c in candidates if settings.get(c["id"], 0.0) >= SETTING_MATCH]
    probs = {k: v for k, v in probs.items() if k == "none" or k in {c["id"] for c in candidates}}
    if not candidates:
        return None, 0.0
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
        if "pexels.com" in url:
            headers = _headers()
        else:
            from .commons import USER_AGENT
            headers = {"User-Agent": USER_AGENT}
        with requests.get(url, stream=True, timeout=90, headers=headers) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
        return dest
    except Exception as exc:  # noqa: BLE001
        log.warning("download failed %s: %s", url[:80], exc)
        dest.unlink(missing_ok=True)
        return None


def photo_clip(photo_url: str, photo_id: str, seconds: float, out_path: Path, whole: bool = False) -> Path | None:
    """Slow Ken Burns zoom over a photo, rendered to a short mp4."""
    img = _download(photo_url, PEXELS_CACHE / f"photo_{photo_id}.jpg")
    if not img:
        return None
    return image_clip(img, seconds, out_path, whole=whole)


def image_clip(img: Path, seconds: float, out_path: Path, whole: bool = False) -> Path | None:
    """Slow Ken Burns zoom over any local image (stock photo or AI-generated). whole=True keeps a landscape photo
    whole (over a blurred copy of itself) instead of cropping its middle third: a real landmark must stay visible."""
    from .frame import size
    W, H = size()
    frames = int((seconds + 0.5) * 30)
    zoom = f"zoompan=z='min(zoom+0.0006,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30,format=yuv420p"
    if whole:
        graph = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"boxblur=28:4,eq=brightness=-0.12[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                 "[bg][fg]overlay=(W-w)/2:(H-h)/2," + zoom)
        filt = ["-filter_complex", graph]
    else:
        filt = ["-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}," + zoom]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(img), *filt,
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
    from .frame import size
    filt = (f"gradients=size={size()[0]}x{size()[1]}:speed=0.015:nb_colors=3:c0={palette[0]}:c1={palette[1]}:c2={palette[2]}"
            f":duration={seconds:.2f}:rate=30,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", filt,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-t", f"{seconds:.2f}", str(out_path)]
    _run(cmd, check=True, text=True)
    return out_path


def footage_for_line(line_text: str, keyword: str, seconds: float, work_dir: Path, index: int,
                     fallback_keyword: str = "", used: set[str] | None = None, named_subject: str = "",
                     credits: list[dict[str, Any]] | None = None, topic: str = "",
                     image_prompt: str = "") -> tuple[Path, str]:
    """Best matching background for one line. Returns (clip path, note for the log)."""
    # Pass 0: a real photo of the specific named thing the line is about. Pass 1: the line's own term, literal
    # match. Pass 2: the video's subject term, same-subject match.
    used = used if used is not None else set()
    from . import imagegen as _ig
    if named_subject and _ig.settings().get("mode") != "only":
        from . import commons
        pick, fit = commons.choose(line_text, named_subject, commons.search(named_subject), used=used)
        if pick:
            ok, seen = _looked_at(line_text, named_subject, pick, topic=topic)
            pick = pick if ok else None
        clip = pick and photo_clip(pick["link"], pick["id"], seconds, work_dir / f"bg_{index}.mp4",
                                   whole=bool(pick.get("landscape")) != _orient_is_landscape())
        if clip:
            used.add(pick["id"])
            if credits is not None:
                credits.append(pick["credit"])
            return clip, (f"photo of {named_subject} '{pick['credit']['title'][:50]}' from Wikimedia Commons "
                          f"({fit:.0%} match){seen}")
    from . import imagegen

    ai = imagegen.settings()
    ai_on = bool(ai.get("enabled")) and ai.get("mode") != "never" and imagegen.available()
    if ai_on and ai.get("mode") in ("always", "only"):
        img, result = _ai_image(line_text, keyword, fallback_keyword, image_prompt, keyword, topic,
                                tries=int(ai.get("tries", 2)))
        clip = img and image_clip(img, seconds, work_dir / f"bg_{index}.mp4")
        if clip:
            return clip, f"AI image ({img.stem.split('_')[0]}) for '{keyword}'{_ai_note(result)}"
        if ai.get("mode") == "only":          # no internet footage at all: the generated background
            clip = generated_background(work_dir / f"bg_{index}.mp4", seconds + 0.5, seed=index)
            return clip, f"generated background (no AI image for '{keyword}')"
    attempts = [(keyword, True)]
    if fallback_keyword:
        attempts.append((fallback_keyword, False))
    attempts.append((keyword, False))
    reuse: tuple[dict[str, Any], float, str, bool] | None = None    # a clip already in this Short: last resort only
    from . import vision
    checks_left = int(vision.config()["max_checks"]) if vision.enabled() else 0
    subject = named_subject or keyword
    for kw, strict in attempts:
        for kind, cands in (("video", video_candidates(kw)), ("photo", photo_candidates(kw))):
            rejected: set[str] = set()
            while True:
                pool = [c for c in cands if c["id"] not in rejected]
                pick, fit = choose(line_text, kw, pool, strict=strict, used=used)
                if not pick or fit < MIN_FIT:
                    break
                if pick["id"] in used:
                    reuse = reuse or (pick, fit, kw, strict)
                    break
                seen = ""
                if checks_left and (pick.get("preview") or kind == "photo"):     # look before downloading
                    checks_left -= 1
                    ok, seen = _looked_at(line_text, subject, pick, topic=topic)
                    if not ok:
                        rejected.add(pick["id"])
                        continue
                clip = _materialize(pick, kind, seconds, work_dir, index)
                if not clip:
                    break
                if checks_left and not seen:                                    # no preview: a frame of the clip
                    checks_left -= 1
                    ok, seen = _looked_at(line_text, subject, pick, clip, topic=topic)
                    if not ok:
                        rejected.add(pick["id"])
                        continue
                stock_shows = (_LAST_LOOK.get("result") or {}).get("shows") if seen else None
                challenger = _ai_challenger(line_text, keyword, fallback_keyword, subject, named_subject, stock_shows,
                                            seconds, work_dir, index, topic, image_prompt)
                if challenger:
                    return challenger
                used.add(pick["id"])
                src = " on Pixabay" if pick.get("source") == "pixabay" else ""
                return clip, (f"{kind} '{pick['description'][:60]}'{src} ({fit:.0%} "
                              f"{'literal' if strict else 'same-subject'} fit, '{kw}'){seen}")
    if reuse and not ai_on:
        pick, fit, kw, strict = reuse
        clip = _materialize(pick, pick.get("kind", "video"), seconds, work_dir, index)
        if clip:
            return clip, f"{pick.get('kind', 'video')} '{pick['description'][:60]}' again (nothing new fit '{kw}')"
    if ai_on:
        img, result = _ai_image(line_text, keyword, fallback_keyword, image_prompt, keyword, topic,
                                tries=int(ai.get("tries", 2)))
        clip = img and image_clip(img, seconds, work_dir / f"bg_{index}.mp4")
        if clip:
            return clip, (f"AI image ({img.stem.split('_')[0]}) for '{keyword}' (nothing on Pexels matched)"
                          f"{_ai_note(result)}")
    clip = generated_background(work_dir / f"bg_{index}.mp4", seconds + 0.5, seed=index)
    return clip, f"generated background (nothing on Pexels matched '{keyword}' or '{fallback_keyword}')"


def _looked_at(line_text: str, subject: str, pick: dict[str, Any], clip: Path | None = None,
               topic: str = "") -> tuple[bool, str]:
    """The image check (vision.py): (use it?, note for the log). In log mode it never rejects."""
    from . import vision
    if not vision.enabled():
        return True, ""
    result = vision.judge(line_text, subject, pick, clip, topic=topic)
    _LAST_LOOK["result"] = result
    if result is None:
        return True, " · image check unavailable"
    ok = vision.verdict(result)
    note = (f" · image check: shows {result['shows']:.0%}, contradicts {result['conflict']:.0%}, "
            f"off-topic {result.get('off_topic', 0.0):.0%}")
    if not ok and vision.config()["mode"] == "log":
        log.info("image check would reject %s '%s'%s", pick["id"], pick.get("description", "")[:60], note)
        return True, note + " (would reject)"
    if not ok:
        log.info("image check rejected %s '%s'%s", pick["id"], pick.get("description", "")[:60], note)
    return ok, note


_LAST_LOOK: dict[str, Any] = {}
_AI_BUDGET = {"left": 0}


def _ai_image(line_text: str, keyword: str, fallback_keyword: str, image_prompt: str, subject: str, topic: str,
              tries: int = 2) -> tuple[Path | None, dict[str, float] | None]:
    """An AI image for the line, looked at by the image check: one that does not show the line is made again (another
    take of the director's scene), up to `tries`; the take that showed the line best is returned."""
    from . import imagegen, vision
    best: tuple[Path | None, dict[str, float] | None] = (None, None)
    for variant in range(max(1, tries)):
        img = imagegen.generate(line_text, keyword, fallback_keyword, prompt=image_prompt, variant=variant)
        if not img:
            continue
        if not vision.enabled():
            return img, None
        result = vision.judge(line_text, subject, {"id": "ai_" + img.stem, "kind": "photo", "local": str(img)}, topic=topic)
        if result is None or vision.verdict(result):
            return img, result
        log.info("AI image take %d does not show %r well enough (shows %.0f%%, contradicts %.0f%%, off-topic %.0f%%)",
                 variant + 1, subject, result["shows"] * 100, result["conflict"] * 100, result.get("off_topic", 0) * 100)
        if best[0] is None or result["shows"] > (best[1] or {}).get("shows", -1):
            best = (img, result)
    return best


def _ai_note(result: dict[str, float] | None) -> str:
    return f" · image check: shows {result['shows']:.0%}" if result else ""


def _ai_challenger(line_text: str, keyword: str, fallback_keyword: str, subject: str, named_subject: str,
                   stock_shows: float | None, seconds: float, work_dir: Path, index: int,
                   topic: str, image_prompt: str = "") -> tuple[Path, str] | None:
    """ai_images.mode = compete: when the best internet clip shows the line only weakly, generate an AI image, have
    the image check judge it the same way, and return it when it shows the line clearly better. Never for a line
    about a real named place or person (an invented picture of a real landmark would mislead viewers)."""
    from . import imagegen, vision
    ai = imagegen.settings()
    if (ai.get("mode") != "compete" or not ai.get("enabled") or named_subject or not vision.enabled()
            or _AI_BUDGET["left"] <= 0 or not imagegen.available()):
        return None
    stock = 0.6 if stock_shows is None else stock_shows      # no image check of the clip: assume a fair match
    if stock >= float(ai["compete_below"]):
        return None
    _AI_BUDGET["left"] -= 1
    img, result = _ai_image(line_text, keyword, fallback_keyword, image_prompt, subject, topic, tries=1)
    if not img:
        return None
    if not result or not vision.verdict(result) or result["shows"] < stock + float(ai["compete_margin"]):
        log.info("AI image lost to the clip for %r (AI shows %s, clip %.0f%%)", keyword,
                 f"{result['shows']:.0%}" if result else "?", stock * 100)
        return None
    clip = image_clip(img, seconds, work_dir / f"bg_{index}.mp4")
    if not clip:
        return None
    return clip, (f"AI image ({img.stem.split('_')[0]}) for '{keyword}': shows {result['shows']:.0%}, beat the best clip "
                  f"({stock:.0%})")


def _materialize(pick: dict[str, Any], kind: str, seconds: float, work_dir: Path, index: int) -> Path | None:
    if kind == "photo":
        return photo_clip(pick["link"], pick["id"], seconds, work_dir / f"bg_{index}.mp4")
    return _download(pick["link"], PEXELS_CACHE / f"{pick['id']}.mp4")


def plan_backgrounds(script: dict[str, Any], words: list[dict[str, Any]], total_seconds: float,
                     source: str, work_dir: Path, credits: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One background segment per script line (or one generated clip for the whole video). Photos that need credit
    (Wikimedia Commons) are appended to `credits`."""
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
    lines = script["lines"]
    # The hook and the closing line rarely name the subject ("And you'd walk right past it") but are about it: they
    # show the video's main named subject, so a Short about one landmark does not end on some other place.
    marked = [ln.get("named_subject", "").strip() for ln in lines if (ln.get("named_subject") or "").strip()]
    main = max(set(marked), key=marked.count) if marked else ""
    named = [(lines[0].get("named_subject") if lines else "") or main, *[ln.get("named_subject", "") for ln in lines], main]
    segments: list[dict[str, Any]] = []
    used: set[str] = set()
    _AI_BUDGET["left"] = int(imagegen.settings().get("max_per_video", 4))
    prompts = script.get("image_prompts") or []
    if use_ai and ai.get("mode") in ("only", "always", "compete") and len(prompts) != len(chunks):
        from .script_gen import image_prompts
        prompts = image_prompts(script)                 # one call: the visual director sees the whole script
        if prompts:
            script["image_prompts"] = prompts
            if (work_dir / "script.json").exists():
                from .storage import save_json
                save_json(work_dir / "script.json", script)
    idx, t = 0, 0.0
    for i, (chunk, kw, subject) in enumerate(zip(chunks, keywords, named)):
        n = len(chunk.split())
        group = words[idx: idx + n]
        idx += n
        end = total_seconds if i == len(chunks) - 1 else (group[-1]["end"] if group else t)
        if not chunk.strip() or end <= t:
            continue
        clip, note = footage_for_line(chunk, kw, end - t, work_dir, i, fallback_keyword=fallback if kw != fallback else "",
                                      used=used, named_subject=(subject or "").strip(), credits=credits,
                                      topic=script.get("title", ""),
                                      image_prompt=prompts[i] if i < len(prompts) else "")
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
