"""Real photos of specific named things from Wikimedia Commons (free licences, credit in the description).

Stock sites have "a church carved in rock" but rarely *the* church the line is about (Bete Giyorgis in Lalibela).
When the script writer marks a line as being about one specific named place, building, person, artwork, ship or
artifact (`named_subject`), Commons is searched for it: millions of freely licensed photos, the same ones Wikipedia
uses, no key needed. Only public-domain, CC0, CC BY and CC BY-SA files are used; each one used is credited in the
video description (author, licence, link), which is what those licences ask for. Images found on Google are mostly
copyrighted, which is why Commons is the source rather than a web image search.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

import requests

from .config import load_config
from .storage import JsonCache

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia asks automated clients to identify themselves with a contact URL.
USER_AGENT = "ShortsEngine/1.0 (https://github.com/bigmo6286/youtube-shorts-automation; personal YouTube Shorts tool)"
_CACHE = JsonCache("commons_search")
DEFAULTS = {"enabled": True, "candidates": 10, "min_match": 0.7, "min_width": 800}
FREE = re.compile(r"^(public domain|pd\b|cc0|cc[- ]by(?:[- ]sa)?(?:[- ][\d.]+)?)", re.I)
NOT_FREE = re.compile(r"\b(nc|nd|non-?commercial|no-?deriv|fair use)\b", re.I)


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("reference_images")) or {})
    return cfg


def _plain(value: Any) -> str:
    """Commons metadata is HTML ('<a href=...>Jane Doe</a>'): keep the text."""
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def free_licence(short_name: str) -> bool:
    name = (short_name or "").strip()
    return bool(FREE.match(name)) and not NOT_FREE.search(name)


def search(subject: str) -> list[dict[str, Any]]:
    """Freely licensed photos whose title or description match `subject`, as footage photo candidates."""
    cfg = config()
    if not cfg["enabled"] or not subject.strip():
        return []
    key = subject.strip().lower()
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
              "gsrsearch": f"{subject} filetype:bitmap", "gsrlimit": int(cfg["candidates"]),
              "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1440,
              "iiextmetadatafilter": "LicenseShortName|Artist|Credit|ImageDescription|Categories|ObjectName"}
    try:
        r = requests.get(API, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("Wikimedia Commons search failed for %r: %s", subject, str(exc)[:120])
        return []
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        licence = _plain((meta.get("LicenseShortName") or {}).get("value"))
        if info.get("mime") not in ("image/jpeg", "image/png") or not free_licence(licence):
            continue
        if int(info.get("width") or 0) < int(cfg["min_width"]):
            continue
        title = re.sub(r"^File:|\.(jpe?g|png)$", "", p.get("title", ""), flags=re.I).replace("_", " ")
        about = _plain((meta.get("ImageDescription") or {}).get("value"))[:240]
        cats = _plain((meta.get("Categories") or {}).get("value")).replace("|", ", ")[:200]
        author = _plain((meta.get("Artist") or {}).get("value"))[:80] or "unknown author"
        out.append({
            "id": f"wc{p['pageid']}", "kind": "photo", "source": "commons",
            "description": f"{title}. {about} (categories: {cats})".strip(),
            "link": info.get("thumburl") or info.get("url"), "page": info.get("descriptionurl", ""),
            "landscape": int(info.get("width") or 0) > int(info.get("height") or 1),
            "credit": {"title": title, "author": author, "licence": licence,
                       "url": info.get("descriptionshorturl") or info.get("descriptionurl", "")},
        })
    _CACHE.set(key, out)
    return out


def choose(line_text: str, subject: str, candidates: list[dict[str, Any]],
           used: set[str] | None = None) -> tuple[dict[str, Any] | None, float]:
    """The photo that really shows `subject`, judged from each file's title, description and categories.
    Code owns the policy: a photo must show the subject itself (not a map, plan, document, another place or a
    detail too small to read as it) at least `min_match`; the best unused one wins."""
    from .config import has_typesafe
    if not candidates:
        return None, 0.0
    used = used or set()
    if not has_typesafe():
        exact = [c for c in candidates if subject.lower() in c["description"].lower() and c["id"] not in used]
        return (exact[0], 0.7) if exact else (None, 0.0)
    from typesafe_sdk import Noul, TypeSafeClient
    state = {"spoken_line": line_text, "named_subject": subject,
             "photos": [{"id": c["id"], "file": c["description"]} for c in candidates]}
    questions = {}
    for c in candidates:
        questions[f"shows_{c['id']}"] = Noul(instructions=(
            f"Judging by its file name, description and categories in `photos`, is the photo with id `{c['id']}` a "
            "photograph of `named_subject` itself, the one the spoken line is about, recognisable as it in a "
            "full-screen video? Answer no for a different place or person with a similar name, a map, plan, "
            "diagram, document, stamp, coin or logo, a museum label, or a small detail that would not read as "
            "the subject."))
    try:
        with TypeSafeClient(timeout=60.0) as client:
            resp = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001
        log.warning("Commons photo check failed (%s); not using reference photos for this line", exc)
        return None, 0.0
    scored = sorted(((float(resp.answers[f"shows_{c['id']}"].noul), c) for c in candidates), key=lambda x: -x[0])
    good = [(p, c) for p, c in scored if p >= float(config()["min_match"])]
    fresh = [(p, c) for p, c in good if c["id"] not in used]
    if fresh:
        return fresh[0][1], fresh[0][0]
    return (good[0][1], good[0][0]) if good else (None, 0.0)


def credit_line(credits: list[dict[str, Any]]) -> str:
    """'Photos: Wikimedia Commons: Bete Giyorgis by A. Savin (CC BY-SA 3.0) https://...'"""
    seen, parts = set(), []
    for c in credits:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        parts.append(f"{c['title'][:60]} by {c['author']} ({c['licence']}) {c['url']}")
    return ("Photos from Wikimedia Commons:\n" + "\n".join(parts)) if parts else ""
