"""AI-generated images for lines where stock footage has nothing.

Providers:
  pollinations  free, no key, ~5 s per image (default)
  together      FLUX.1 schnell via api.together.xyz (TOGETHER_API_KEY)
  huggingface   FLUX.1 schnell (or any text-to-image model) via the HF Inference API (HF_TOKEN)
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


_OVERRIDE: dict[str, str | None] = {"mode": None}


def override(mode: str | None) -> None:
    """One production's mode (produce --ai-images-only sets "only"); None = the configured mode."""
    _OVERRIDE["mode"] = mode


def settings() -> dict[str, Any]:
    cfg = dict((load_config().get("production") or {}).get("ai_images") or {})
    if _OVERRIDE["mode"]:
        cfg["mode"], cfg["enabled"] = _OVERRIDE["mode"], True
    cfg.setdefault("enabled", True)
    cfg.setdefault("mode", "fallback")        # fallback | compete | always | only (no internet footage) | never
    cfg.setdefault("compete_below", 0.85)     # compete: an AI image is tried when the best clip shows the line less than this
    cfg.setdefault("compete_margin", 0.10)    # ...and used when it shows the line at least this much better
    cfg.setdefault("max_per_video", 4)
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
    if p == "huggingface":
        return bool(env("HF_TOKEN"))
    return False


def build_prompt(line_text: str, keyword: str, subject: str, style: str) -> str:
    """A clear scene of the subject (footage term, with the video's subject for grounding), the sentence as
    context, then the look. Macro / close-up wording is softened: it produces distorted anatomy in generators."""
    scene = re.sub(r"\b(close[- ]?up|macro|extreme)\b", "", keyword, flags=re.I).strip(" ,") or subject.strip()
    if subject and subject.lower() not in scene.lower():
        scene = f"{scene} ({subject})"
    context = re.sub(r"\s+", " ", line_text).strip()
    return f"A clear photograph of {scene}, illustrating: {context}. {style}"


def _dims(w: int, h: int) -> tuple[int, int]:
    """Provider image size: portrait for Shorts, the same size turned for a landscape long video."""
    from .frame import landscape
    return (h, w) if landscape() else (w, h)


def _style_for_frame(style: str) -> str:
    from .frame import landscape
    if not landscape():
        return style
    return re.sub(r"vertical(?: 9:16)?", "horizontal 16:9", style)


def _cache_path(prompt: str, provider: str) -> Path:
    AIIMG_DIR.mkdir(parents=True, exist_ok=True)
    return AIIMG_DIR / f"{provider}_{hashlib.sha1(prompt.encode()).hexdigest()[:16]}.jpg"


POLLINATIONS_GAP = 65.0       # free tier: "1 image every 45 seconds per IP"; at 46 s it still answered 402 FREE_LIMIT


def _pollinations_turn() -> None:
    """Without a pollen key: wait until 65 s after this machine's last request (shared by every channel's console
    through a file). With POLLINATIONS_TOKEN there is no such limit."""
    if env("POLLINATIONS_TOKEN"):
        return
    stamp = AIIMG_DIR / ".pollinations_last"
    try:
        last = float(stamp.read_text())
    except (OSError, ValueError):
        last = 0.0
    wait = last + POLLINATIONS_GAP - time.time()
    if wait > 0:
        log.info("free AI image generator: waiting %.0f s for its turn (1 image per 45 s)", wait)
        time.sleep(wait)
    AIIMG_DIR.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str(time.time()))


POLLEN_MODEL = "black-forest-labs/flux.2-klein-4b"   # 0.005 pollen an image; scored best on prompt-following here


def _pollen(prompt: str, dest: Path) -> Path:
    """Pollinations with a pollen key (POLLINATIONS_TOKEN, sk_...): the gen.pollinations.ai API, no rate limit, a choice
    of models (ai_images.pollinations_model) and no watermark."""
    model = settings().get("pollinations_model") or POLLEN_MODEL
    w, h = _dims(1080, 1920)
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(f"https://gen.pollinations.ai/image/{quote(prompt)}",
                             params={"model": model, "width": w, "height": h, "seed": 11 + attempt},
                             headers={"Authorization": f"Bearer {env('POLLINATIONS_TOKEN')}"}, timeout=180)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                dest.write_bytes(r.content)
                return dest
            last = RuntimeError(f"HTTP {r.status_code} {r.text[:200]}")
            if r.status_code in (401, 402, 403):        # bad key, no pollen left: no point retrying
                break
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"pollinations (key) failed: {last}")


def _pollinations(prompt: str, dest: Path) -> Path:
    if env("POLLINATIONS_TOKEN"):
        try:
            return _pollen(prompt, dest)
        except RuntimeError as exc:
            log.warning("%s; using the free tier", str(exc)[:200])
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}"
    last: Exception | None = None
    for attempt in range(3):
        try:
            _pollinations_turn()
            w, h = _dims(1080, 1920)
            token = env("POLLINATIONS_TOKEN")
            try:
                r = requests.get(url, params={"width": w, "height": h, "nologo": "true", "seed": 11 + attempt,
                                              "model": "flux", "enhance": "false"}, timeout=120,
                                 headers={"Authorization": f"Bearer {token}"} if token else None)
            finally:
                (AIIMG_DIR / ".pollinations_last").write_text(str(time.time()))   # the 45 s count from the answer
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                dest.write_bytes(r.content)
                _crop_bottom(dest, 0.07)      # the free service stamps a small logo in the bottom-right corner
                return dest
            last = RuntimeError(f"HTTP {r.status_code} {r.text[:160] if r.status_code != 200 else ''}")
            if r.status_code == 402 and "FREE_LIMIT" in r.text:      # another request got in first: wait a turn
                continue
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"pollinations failed: {last}")


def _crop_bottom(path: Path, fraction: float) -> None:
    """Cut `fraction` off the bottom of an image in place (used to remove a provider watermark)."""
    import subprocess

    from .tools import run as _run, ensure_ffmpeg_on_path

    ensure_ffmpeg_on_path()
    tmp = path.with_suffix(".crop.jpg")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", f"crop=iw:ih*{1 - fraction:.3f}:0:0",
           "-q:v", "2", str(tmp)]
    try:
        _run(cmd, check=True, text=True)
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not crop watermark: %s", exc)
        tmp.unlink(missing_ok=True)


def _together(prompt: str, dest: Path) -> Path:
    model = settings().get("model") or "black-forest-labs/FLUX.1-schnell-Free"
    r = requests.post("https://api.together.xyz/v1/images/generations",
                      headers={"Authorization": f"Bearer {env('TOGETHER_API_KEY')}"},
                      json={"model": model, "prompt": prompt, "negative_prompt": NEGATIVE, "width": _dims(768, 1344)[0], "height": _dims(768, 1344)[1],
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
                      json={"model": model, "prompt": prompt, "size": "x".join(map(str, _dims(1024, 1536))), "n": 1, "quality": "medium"}, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"openai {r.status_code}: {r.text[:200]}")
    data = r.json()["data"][0]
    if data.get("b64_json"):
        dest.write_bytes(base64.b64decode(data["b64_json"]))
    else:
        dest.write_bytes(requests.get(data["url"], timeout=120).content)
    return dest


HF_DEFAULT_MODEL = "black-forest-labs/FLUX.1-schnell"


def _huggingface(prompt: str, dest: Path) -> Path:
    """Hugging Face Inference Providers via the official client: `provider="auto"` routes the model to
    whichever partner serves it (fal-ai, nscale, ...) and bills the monthly HF credits. Errors carry the
    HTTP status, so a 402 (credits exhausted) or 429 (rate limit) is recognised by _looks_like_limit."""
    from huggingface_hub import InferenceClient

    model = settings().get("model") or HF_DEFAULT_MODEL
    client = InferenceClient(api_key=env("HF_TOKEN"), provider="auto", timeout=180)
    try:
        w, h = _dims(768, 1344)
        image = client.text_to_image(prompt, model=model, width=w, height=h, num_inference_steps=4)
    except Exception as exc:  # noqa: BLE001 - normalise so the status code is visible in the message
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise RuntimeError(f"huggingface {status or ''} {str(exc)[:200]}".strip()) from exc
    image.convert("RGB").save(dest, "JPEG", quality=92)
    return dest


PROVIDERS = {"pollinations": _pollinations, "together": _together, "openai": _openai, "huggingface": _huggingface}


_PAUSED_UNTIL: dict[str, float] = {}     # provider -> time until which we skip it (after a limit / credit error)
PAUSE_SECONDS = 3600


def _looks_like_limit(exc: Exception) -> bool:
    """Quota / credit / rate-limit errors (worth pausing the provider), not connection failures."""
    msg = str(exc).lower()
    return any(k in msg for k in ("429", "402", "rate limit", "rate-limit", "quota", "credit", "too many requests",
                                  "insufficient", "billing", "monthly included"))


LIGHTING_WORDS = re.compile(r"\b(natural light(ing)?|well[- ]lit|natural colou?rs|soft daylight|daylight)\b,?\s*", re.I)


def _look(style: str) -> str:
    """The style's look without its lighting words: a director's prompt sets the light the scene really has (the
    fixed "natural light" made a pitch-black deep sea look sunlit)."""
    return re.sub(r"\s*,\s*,", ",", LIGHTING_WORDS.sub("", style)).strip(" ,")


VARIANTS = ["", "", " A wider view of the same scene."]


def generate(line_text: str, keyword: str, subject: str = "", prompt: str = "", variant: int = 0) -> Path | None:
    """Return a cached or freshly generated image for this line (portrait, or landscape for a long video), or None.
    `prompt` is the visual director's description of the scene (script_gen.image_prompts); without one the prompt is
    built from the footage term. variant > 0 asks for another take of the same scene.
    The configured provider is tried first; if it fails (limits, credit, outage) the free keyless
    generator takes over, and a provider that hit a limit is skipped for an hour."""
    cfg = settings()
    style = _style_for_frame(cfg.get("style") or DEFAULT_STYLE)
    if prompt:
        scene = prompt.strip().rstrip(".")
        if variant % len(VARIANTS) == 1:
            # the first take missed its subject: free FLUX models follow the start of a prompt best, so it goes first
            focus = re.sub(r"\b(close[- ]?up|macro|extreme|wide shot)\b", "", keyword, flags=re.I).strip(" ,")
            scene = f"{focus}, clearly visible and in sharp focus in the center of the frame. {scene}"
        prompt = f"{scene}.{VARIANTS[variant % len(VARIANTS)]} {_look(style)}"
    else:
        prompt = build_prompt(line_text, keyword, subject, style) + VARIANTS[variant % len(VARIANTS)]
    order = []
    if available(cfg["provider"]) and _PAUSED_UNTIL.get(cfg["provider"], 0) < time.time():
        order.append(cfg["provider"])
    if "pollinations" not in order:
        order.append("pollinations")
    for provider in order:
        keyed = provider == "pollinations" and env("POLLINATIONS_TOKEN")
        # a keyed model draws differently from the free tier: its images are cached apart
        dest = _cache_path(prompt + (f"|{cfg.get('pollinations_model') or POLLEN_MODEL}" if keyed else ""), provider)
        if dest.exists() and dest.stat().st_size > 1000:
            return dest
        t = time.time()
        try:
            PROVIDERS[provider](prompt, dest)
        except Exception as exc:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            if provider != "pollinations" and _looks_like_limit(exc):
                _PAUSED_UNTIL[provider] = time.time() + PAUSE_SECONDS
                log.warning("AI image provider %s hit a limit (%s); using the free generator for the next hour", provider, str(exc)[:120])
            else:
                log.warning("AI image (%s) failed for %r: %s", provider, keyword, str(exc)[:160])
            continue
        log.info("AI image (%s) for %r in %.1fs", provider, keyword, time.time() - t)
        try:
            from .usage import POLLEN_PRICES, record
            model = cfg.get("pollinations_model") or POLLEN_MODEL
            record(images=1, pollen=POLLEN_PRICES.get(model, 0.005) if (provider == "pollinations" and env("POLLINATIONS_TOKEN")) else 0)
        except Exception:  # noqa: BLE001
            pass
        return dest
    return None
