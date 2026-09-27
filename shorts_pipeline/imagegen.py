"""AI-generated images for lines where stock footage has nothing.

Providers:
  pollinations  free, no key, ~5 s per image (default)
  together      FLUX.1 schnell via api.together.xyz (TOGETHER_API_KEY)
  openai        gpt-image-1 via api.openai.com (OPENAI_API_KEY)
Images are cached under data/cache/aiimg by prompt hash.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import env, load_config
from .storage import CACHE_DIR

log = logging.getLogger(__name__)

AIIMG_DIR = CACHE_DIR / "aiimg"
DEFAULT_STYLE = ("documentary photograph, well lit, natural colors, realistic, sharp detail, wide enough to see the whole subject, "
                 "vertical 9:16 composition, no text, no watermark, no logo")
NEGATIVE = "text, watermark, logo, caption, blurry, deformed, cartoon, gore, extreme close-up"


def settings() -> dict[str, Any]:
    cfg = dict((load_config().get("production") or {}).get("ai_images") or {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("mode", "fallback")        # fallback | always | never
    cfg.setdefault("provider", "pollinations")
    cfg.setdefault("style", DEFAULT_STYLE)
    return cfg


def available(provider: str | None = None) -> bool:
    p = provider or settings()["provider"]
    if p == "pollinations":
        return True
    if p == "together":
        return bool(env("TOGETHER_API_KEY"))
    if p == "openai":
        return bool(env("OPENAI_API_KEY"))
    return False


def build_prompt(line_text: str, keyword: str, subject: str, style: str) -> str:
    """A clear scene of the subject (footage term, with the video's subject for grounding), the sentence as
    context, then the look. Macro / close-up wording is softened: it produces distorted anatomy in generators."""
    scene = re.sub(r"\b(close[- ]?up|macro|extreme)\b", "", keyword, flags=re.I).strip(" ,") or subject.strip()
    if subject and subject.lower() not in scene.lower():
        scene = f"{scene} ({subject})"
    context = re.sub(r"\s+", " ", line_text).strip()
    return f"A clear photograph of {scene}, illustrating: {context}. {style}"


def _cache_path(prompt: str, provider: str) -> Path:
    AIIMG_DIR.mkdir(parents=True, exist_ok=True)
    return AIIMG_DIR / f"{provider}_{hashlib.sha1(prompt.encode()).hexdigest()[:16]}.jpg"


def _pollinations(prompt: str, dest: Path) -> Path:
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}"
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, params={"width": 1080, "height": 1920, "nologo": "true", "seed": 11 + attempt,
                                          "model": "flux", "enhance": "false"}, timeout=120)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                dest.write_bytes(r.content)
                _crop_bottom(dest, 0.07)      # the free service stamps a small logo in the bottom-right corner
                return dest
            last = RuntimeError(f"HTTP {r.status_code} {r.headers.get('content-type', '')}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"pollinations failed: {last}")


def _crop_bottom(path: Path, fraction: float) -> None:
    """Cut `fraction` off the bottom of an image in place (used to remove a provider watermark)."""
    import subprocess

    from .tools import ensure_ffmpeg_on_path

    ensure_ffmpeg_on_path()
    tmp = path.with_suffix(".crop.jpg")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", f"crop=iw:ih*{1 - fraction:.3f}:0:0",
           "-q:v", "2", str(tmp)]
    try:
        subprocess.run(cmd, check=True)
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not crop watermark: %s", exc)
        tmp.unlink(missing_ok=True)


def _together(prompt: str, dest: Path) -> Path:
    model = settings().get("model") or "black-forest-labs/FLUX.1-schnell-Free"
    r = requests.post("https://api.together.xyz/v1/images/generations",
                      headers={"Authorization": f"Bearer {env('TOGETHER_API_KEY')}"},
                      json={"model": model, "prompt": prompt, "negative_prompt": NEGATIVE, "width": 768, "height": 1344,
                            "steps": 4, "n": 1, "response_format": "b64_json"}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"together {r.status_code}: {r.text[:200]}")
    data = r.json()["data"][0]
    if data.get("b64_json"):
        dest.write_bytes(base64.b64decode(data["b64_json"]))
    else:
        dest.write_bytes(requests.get(data["url"], timeout=120).content)
    return dest


def _openai(prompt: str, dest: Path) -> Path:
    model = settings().get("model") or "gpt-image-1"
    r = requests.post("https://api.openai.com/v1/images/generations",
                      headers={"Authorization": f"Bearer {env('OPENAI_API_KEY')}"},
                      json={"model": model, "prompt": prompt, "size": "1024x1536", "n": 1, "quality": "medium"}, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"openai {r.status_code}: {r.text[:200]}")
    data = r.json()["data"][0]
    if data.get("b64_json"):
        dest.write_bytes(base64.b64decode(data["b64_json"]))
    else:
        dest.write_bytes(requests.get(data["url"], timeout=120).content)
    return dest


PROVIDERS = {"pollinations": _pollinations, "together": _together, "openai": _openai}


def generate(line_text: str, keyword: str, subject: str = "") -> Path | None:
    """Return a cached or freshly generated portrait image for this line, or None on failure."""
    cfg = settings()
    provider = cfg["provider"] if available(cfg["provider"]) else "pollinations"
    prompt = build_prompt(line_text, keyword, subject, cfg.get("style") or DEFAULT_STYLE)
    dest = _cache_path(prompt, provider)
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    t = time.time()
    try:
        PROVIDERS[provider](prompt, dest)
    except Exception as exc:  # noqa: BLE001
        log.warning("AI image (%s) failed for %r: %s", provider, keyword, exc)
        dest.unlink(missing_ok=True)
        return None
    log.info("AI image (%s) for %r in %.1fs", provider, keyword, time.time() - t)
    return dest
