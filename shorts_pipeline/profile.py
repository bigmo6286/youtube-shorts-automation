"""Channel style profiles: paste a YouTube channel, get videos in its style.

`add` pulls the channel's most-viewed Shorts (yt-dlp, no key), judges them with TypeSafe (format, topic,
hook, the same taxonomy as the trend ranking), and asks Claude to distil a style guide from the
transcripts. `blueprint_for` turns that into a blueprint the normal produce pipeline understands, so
scripts carry the channel's structure, pacing and voice (never its exact words).
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from pydantic import BaseModel, Field

from .config import DATA_DIR, load_config
from .storage import load_json, save_json

log = logging.getLogger(__name__)

PROFILES_DIR = DATA_DIR / "profiles"


class StyleGuide(BaseModel):
    voice_and_tone: str = Field(description="How the narrator sounds: formal/casual, energy, humour, person (I/you/we), 1-3 sentences")
    hook_patterns: list[str] = Field(description="3-5 concrete patterns their first sentences follow, with a short example each")
    structure: str = Field(description="How a typical video is built from hook to ending, 2-4 sentences")
    pacing_and_sentences: str = Field(description="Sentence length, rhythm, use of questions, pauses, repetition")
    vocabulary_and_phrases: list[str] = Field(description="5-10 recurring words, phrases or verbal tics (do not include brand names unless they are the channel's own)")
    ending_and_cta: str = Field(description="How videos end and what call to action they use")
    dos: list[str] = Field(description="4-6 rules a writer must follow to sound like this channel")
    donts: list[str] = Field(description="3-5 things this channel never does")


def handle_from(url_or_handle: str) -> str:
    s = url_or_handle.strip()
    m = re.search(r"youtube\.com/(@[\w.\-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/channel/(UC[\w\-]+)", s)
    if m:
        return m.group(1)
    if s.startswith("UC") and len(s) >= 20:
        return s
    return s if s.startswith("@") else "@" + s


def _slug(handle: str) -> str:
    return re.sub(r"[^\w\-]+", "_", handle).strip("_").lower()


def profile_path(handle: str) -> Path:
    return PROFILES_DIR / f"{_slug(handle)}.json"


def list_profiles() -> list[dict[str, Any]]:
    if not PROFILES_DIR.exists():
        return []
    out = []
    for p in sorted(PROFILES_DIR.glob("*.json")):
        d = load_json(p)
        if d:
            out.append({k: d.get(k) for k in ("handle", "channel", "videos", "top_formats", "top_topics", "typical_seconds", "created")})
    return out


def load_profile(handle: str) -> dict[str, Any] | None:
    for cand in (profile_path(handle), profile_path(handle_from(handle))):
        d = load_json(cand)
        if d:
            return d
    # allow a loose match on the slug
    if PROFILES_DIR.exists():
        want = _slug(handle).lstrip("_")
        for p in PROFILES_DIR.glob("*.json"):
            if want and want in p.stem:
                return load_json(p)
    return None


def add_profile(url_or_handle: str, *, max_videos: int = 24, min_views: int = 1000) -> dict[str, Any]:
    from . import fetch, judge
    from .judge import canonical_topic

    cfg = load_config()["discovery"]
    fetch.configure(cfg)
    handle = handle_from(url_or_handle)
    base = f"https://www.youtube.com/channel/{handle}" if handle.startswith("UC") else f"https://www.youtube.com/{handle}"
    max_videos = max(4, min(int(max_videos), 150))
    log.info("profile: reading %s/shorts", base)
    flat = fetch._flat_entries(f"{base}/shorts", max(120, max_videos * 2))
    if not flat:
        raise RuntimeError(f"no Shorts found for {handle}; check the handle or channel id")
    flat.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
    picked = [e for e in flat if (e.get("view_count") or 0) >= min_views][:max_videos]
    if len(picked) < max_videos:                 # small channels: top up with the next most-viewed Shorts
        seen = {e["id"] for e in picked}
        picked += [e for e in flat if e["id"] not in seen][: max_videos - len(picked)]
    log.info("profile: %d Shorts listed, fetching metadata and transcripts for the top %d (about %d s per video, "
             "YouTube allows roughly 300 fetches an hour)", len(flat), len(picked), 2)
    shorts = fetch.enrich(picked, max_candidates=max_videos, workers=min(3, cfg.get("workers", 3)),
                          want_transcript=True, transcript_chars=1200)
    shorts = [s for s in shorts if fetch.is_short(s, cfg["max_duration_seconds"])]
    if not shorts:
        raise RuntimeError("could not fetch metadata for that channel's Shorts (rate limit?)")
    channel_name = shorts[0].get("channel") or handle
    for s in shorts:
        if judge.has_typesafe():
            try:
                s["judgment"] = judge.judge_short(s)
            except Exception as exc:  # noqa: BLE001
                log.warning("judgment failed for %s: %s", s["id"], exc)
    judged = [s for s in shorts if s.get("judgment")]
    views = lambda s: float(s.get("view_count") or 0)  # noqa: E731
    fmt_w: dict[str, float] = defaultdict(float)
    top_w: dict[str, float] = defaultdict(float)
    hook_w: dict[str, float] = defaultdict(float)
    pair_w: dict[str, float] = defaultdict(float)
    for s in judged:
        j = s["judgment"]
        w = 1.0 + (views(s) ** 0.5) / 100.0            # popular videos weigh more, but not overwhelmingly
        fmt_w[j["format"]["choice"]] += w
        top_w[canonical_topic(j["topic"]["choice"])] += w
        pair_w[f"{j['format']['choice']}|{canonical_topic(j['topic']['choice'])}"] += w
        for h, p in j["hook_style"]["probabilities"].items():
            hook_w[h] += p * w
    def ranked(d: dict[str, float], n: int) -> list[dict[str, Any]]:
        total = sum(d.values()) or 1.0
        return [{"name": k, "share": round(v / total, 3)} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]
    exemplars = sorted(shorts, key=views, reverse=True)          # every analysed Short, most viewed first
    style = _style_guide(channel_name, exemplars[:10])
    profile = {
        "handle": handle, "channel": channel_name, "created": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "videos": len(shorts), "judged": len(judged),
        "typical_seconds": int(median(s.get("duration") or 30 for s in shorts)),
        "top_formats": ranked(fmt_w, 5), "top_topics": ranked(top_w, 6), "top_hooks": ranked(hook_w, 4), "top_pairs": ranked(pair_w, 6),
        "exemplars": [{"id": s["id"], "title": s["title"], "url": s["url"], "views": s.get("view_count", 0),
                       "duration": s.get("duration"), "transcript": (s.get("transcript") or "")[:900],
                       "likes": s.get("like_count") or 0, "uploaded": s.get("upload_date") or s.get("published") or "",
                       "format": (s.get("judgment") or {}).get("format", {}).get("choice"),
                       "topic": canonical_topic((s.get("judgment") or {}).get("topic", {}).get("choice", "")),
                       "hook_style": (s.get("judgment") or {}).get("hook_style", {}).get("choice")} for s in exemplars],
        "style_guide": style,
    }
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    save_json(profile_path(handle), profile)
    log.info("profile saved: %s (%s), %d videos, formats %s, topics %s", handle, channel_name, len(shorts),
             [f["name"] for f in profile["top_formats"][:3]], [t["name"] for t in profile["top_topics"][:3]])
    return profile


def _style_guide(channel_name: str, exemplars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Ask Claude to describe HOW this channel writes, from its transcripts (structure and voice, not content)."""
    from . import script_gen

    texts = [e for e in exemplars if (e.get("transcript") or "").strip()]
    if not texts:
        log.warning("no transcripts available; the profile will carry formats and topics only")
        return None
    samples = "\n\n".join(f"[{i + 1}] {e['title']!r} ({e.get('view_count', 0):,} views)\n{e['transcript'][:900]}" for i, e in enumerate(texts[:8]))
    system = ("You are an expert short-form video script analyst. From transcripts of a YouTube Shorts channel you "
              "describe its WRITING STYLE so another writer can imitate the style with new subjects. Describe patterns, "
              "never copy sentences. Output only the JSON object.")
    prompt = (f"Channel: {channel_name}\n\nTranscripts of its most-viewed Shorts:\n\n{samples}\n\n"
              "Describe the channel's style as the JSON object.")
    try:
        backend = script_gen.pick_backend(load_config()["production"].get("script_backend", "auto"))
        if backend == "api":
            import anthropic
            client = anthropic.Anthropic()
            resp = client.messages.parse(model=script_gen.MODEL, max_tokens=3000, system=system,
                                         messages=[{"role": "user", "content": prompt}], output_format=StyleGuide)
            return resp.parsed_output.model_dump()
        from . import claude_code_backend
        data = claude_code_backend.generate_json(system, prompt, StyleGuide.model_json_schema())
        return StyleGuide.model_validate(data).model_dump()
    except Exception as exc:  # noqa: BLE001
        log.warning("style guide failed (%s); the profile will carry formats, topics and exemplars only", str(exc)[:160])
        return None


def ensure_style_guide(profile: dict[str, Any]) -> dict[str, Any]:
    """Retry the style guide once if the analysis saved the profile without one (Claude was unavailable)."""
    if profile.get("style_guide") or not profile.get("exemplars"):
        return profile
    style = _style_guide(profile.get("channel") or profile.get("handle", ""), profile["exemplars"][:10])
    if style:
        profile["style_guide"] = style
        save_json(profile_path(profile["handle"]), profile)
        log.info("style guide for %s written on retry", profile.get("channel") or profile.get("handle"))
    return profile


def blueprint_for(profile: dict[str, Any], state: dict[str, Any] | None = None,
                  exemplar_id: str | None = None) -> dict[str, Any]:
    """A blueprint in this channel's style. Rotates through the channel's top format x topic pairs by share,
    or, with `exemplar_id`, models the video on that one Short of the channel (same subject, format and hook)."""
    import random

    if exemplar_id:
        model = next((e for e in profile.get("exemplars", []) if e.get("id") == exemplar_id), None)
        if not model:
            raise RuntimeError(f"no video {exemplar_id!r} in the profile of {profile.get('handle')}; analyse the channel again")
        fmt = model.get("format") if model.get("format") not in (None, "other", "uncertain") else (profile.get("top_formats") or [{"name": "storytime"}])[0]["name"]
        topic = model.get("topic") if model.get("topic") not in (None, "other") else (profile.get("top_topics") or [{"name": "other"}])[0]["name"]
        hook = model.get("hook_style") if model.get("hook_style") not in (None, "no_hook", "visual_only") else "curiosity_gap"
        others = [e for e in profile.get("exemplars", []) if e.get("id") != exemplar_id and e.get("format") == fmt][:2]
        return {
            "format": fmt, "topic": topic, "hook_style": hook,
            "why_it_works": (f"a remake of {model.get('title')!r} by {profile.get('channel') or profile.get('handle')} "
                             f"({int(model.get('views') or 0):,} views): same subject and structure, written fresh"),
            "exemplars": [model] + others, "model_video": model,
            "style_guide": profile.get("style_guide"), "style_of": profile.get("channel") or profile.get("handle"),
            "typical_seconds": model.get("duration") or profile.get("typical_seconds"),
        }
    pairs = profile.get("top_pairs") or []
    if pairs:
        pick = random.choices(pairs, weights=[max(0.05, p["share"]) for p in pairs], k=1)[0]
        fmt, topic = pick["name"].split("|", 1)
    else:
        fmt = (profile.get("top_formats") or [{"name": "storytime"}])[0]["name"]
        topic = (profile.get("top_topics") or [{"name": "other"}])[0]["name"]
    hook = (profile.get("top_hooks") or [{"name": "curiosity_gap"}])[0]["name"]
    if hook in ("no_hook", "visual_only") and len(profile.get("top_hooks") or []) > 1:
        hook = profile["top_hooks"][1]["name"]
    exemplars = [e for e in profile.get("exemplars", []) if e.get("format") == fmt] or profile.get("exemplars", [])
    return {
        "format": fmt, "topic": topic, "hook_style": hook,
        "why_it_works": f"the style of {profile.get('channel') or profile.get('handle')}: its most-viewed Shorts are "
                        f"{fmt} videos about {topic}, opening with a {hook} hook, around {profile.get('typical_seconds', 40)} seconds",
        "exemplars": exemplars[:3],
        "style_guide": profile.get("style_guide"),
        "style_of": profile.get("channel") or profile.get("handle"),
        "typical_seconds": profile.get("typical_seconds"),
    }
