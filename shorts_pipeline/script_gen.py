"""Write a new Short's script with Claude, following a blueprint, then QA it with TypeSafe."""
from __future__ import annotations

import logging
import re
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from .judge import judge_script

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"


class ScriptLine(BaseModel):
    text: str = Field(description="One spoken sentence, max about 18 words, natural for text-to-speech")
    visual_keyword: str = Field(description=(
        "Stock-footage search term for THIS line: a literal, filmable scene in 2-5 plain words, the kind of clip "
        "a stock site actually has (e.g. 'octopus swimming underwater', 'woman brushing teeth', 'city traffic at night'). "
        "Name the concrete subject of the sentence; never abstract ideas, emotions or metaphors."))


class ShortScript(BaseModel):
    title: str = Field(description="YouTube title: 40-70 characters, hard maximum 100, no hashtags, includes the hook idea, no clickbait lies")
    hook: str = Field(description="The first spoken sentence. Must stop the scroll within 2 seconds.")
    lines: list[ScriptLine] = Field(description="The body, in order, after the hook")
    cta: str = Field(description="One short closing line (question to the viewer or a soft follow ask)")
    description: str = Field(description="YouTube description: 1-3 sentences, then the hashtags on their own line")
    hashtags: list[str] = Field(description="3-6 hashtags without the # sign, first one shorts, never more than 15")
    visual_fallback: str = Field(description=(
        "One literal stock-footage term for the video's overall subject (2-4 plain words), used when a line's own term finds nothing"))
    thumbnail_text: str = Field(description=(
        "3-6 punchy words for the thumbnail, the most curiosity-provoking idea in the video, no punctuation needed, e.g. 'ONE HEART STOPS'"))


SYSTEM = """You write scripts for faceless YouTube Shorts: an AI voice reads the script over stock footage with
big on-screen captions. Rules:
- Total spoken length must fit the target seconds at about 2.6 words per second.
- Every line is one short sentence. No "number one, number two" unless the format is a listicle.
- The hook is the first sentence and must be specific and surprising, in the requested hook style.
- Everything factual must be true and verifiable from your own knowledge; if unsure, say "reportedly" or choose a
  different fact. You cannot look anything up, so never try to search or browse.
- No medical, legal or financial advice stated as fact. No hate, harassment, or dangerous instructions.
- Do not copy the exemplar Shorts. Use them only to understand the pacing and format that works.
- Write for speech: contractions, plain words, no emojis, no markdown."""


def _prompt(blueprint: dict[str, Any], target_seconds: int, angle: str | None, avoid: list[str] | None = None) -> str:
    words = int(target_seconds * 2.6)
    exemplars = "\n".join(
        f"- Title: {e['title']!r}; opening transcript: {e.get('transcript', '')[:250]!r}"
        for e in blueprint.get("exemplars", [])
    ) or "- (none)"
    own = blueprint.get("source") == "channel"
    exemplar_label = ("Your channel's own best videos of this kind. Their SUBJECTS ARE TAKEN: copy the pacing and "
                      "structure, never the story, fact, person or experiment" if own else
                      "Trending exemplars (for pacing only, do not copy)")
    angle_line = f"Specific angle or subject to use: {angle}\n" if angle else "Pick a fresh, specific subject inside the topic.\n"
    if avoid:
        angle_line += ("Videos already made. Every subject below is TAKEN: do not reuse any of these stories, facts, "
                       "experiments, people, animals or places, even reworded or from a new angle:\n"
                       + "\n".join(f"- {t}" for t in avoid[:60]) + "\n")
    style = blueprint.get("style_guide")
    if style:
        angle_line += (
            f"\nWrite in the STYLE of the channel {blueprint.get('style_of', '')}. Imitate how they write, with a new subject and "
            "your own sentences; never reuse their lines.\n"
            f"- Voice and tone: {style.get('voice_and_tone', '')}\n"
            f"- Hook patterns they use: {'; '.join(style.get('hook_patterns') or [])}\n"
            f"- Structure: {style.get('structure', '')}\n"
            f"- Pacing: {style.get('pacing_and_sentences', '')}\n"
            f"- Recurring phrases: {', '.join(style.get('vocabulary_and_phrases') or [])}\n"
            f"- Endings: {style.get('ending_and_cta', '')}\n"
            f"- Do: {'; '.join(style.get('dos') or [])}\n"
            f"- Never: {'; '.join(style.get('donts') or [])}\n"
        )
    return (
        f"Format: {blueprint['format']}\nTopic: {blueprint['topic']}\nHook style: {blueprint['hook_style']}\n"
        f"Why this works right now: {blueprint.get('why_it_works', '')}\n"
        f"{angle_line}"
        f"Target length: about {target_seconds} seconds, roughly {words} spoken words in total.\n\n"
        f"{exemplar_label}:\n{exemplars}\n\n"
        "Write the script now."
    )


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# YouTube limits: title 100 chars (Shorts show ~70 before truncating), description 5000, at most 15 hashtags
# (more than 15 and YouTube ignores them all). Hashtags in the title count toward the 100.
TITLE_MAX = 100
TITLE_TARGET = 70
DESCRIPTION_MAX = 5000
HASHTAGS_MAX = 15


def _shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-–—.!?")


def finalize_metadata(script: dict[str, Any]) -> dict[str, Any]:
    """Make title / description / hashtags safe to paste into YouTube as-is."""
    title = re.sub(r"#\w+", "", script.get("title") or "").strip() or (script.get("hook") or "Untitled Short")
    title = _shorten(title, TITLE_MAX)
    script["title"] = title
    script["title_over_target"] = len(title) > TITLE_TARGET

    tags: list[str] = []
    for t in script.get("hashtags") or []:
        t = re.sub(r"[^\w]", "", str(t)).strip()
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    if "shorts" not in {t.lower() for t in tags}:
        tags.insert(0, "shorts")
    script["hashtags"] = tags[:HASHTAGS_MAX]

    desc = (script.get("description") or "").strip()
    present = {m.lower() for m in re.findall(r"#(\w+)", desc)}
    extra = [t for t in script["hashtags"] if t.lower() not in present]
    if len(present) + len(extra) > HASHTAGS_MAX:                # keep the description itself under 15 tags too
        extra = extra[: max(0, HASHTAGS_MAX - len(present))]
    if extra:
        desc = (desc + "\n\n" if desc else "") + " ".join("#" + t for t in extra)
    script["description"] = desc[:DESCRIPTION_MAX]
    return script


def custom_script(text: str, *, title: str = "", description: str = "", hashtags: list[str] | None = None,
                  keywords: list[str] | None = None, qa_blueprint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wrap a user-written script in the same structure the generator produces, so voice, captions,
    footage and render need no special casing. Nothing is rewritten; TypeSafe QA runs for information only."""
    text = re.sub(r"\s+", " ", text.replace("\r", "\n")).strip()
    if not text:
        raise ValueError("The script is empty.")
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) == 1:
        sentences = [text]
    hook = sentences[0]
    cta = sentences[-1] if len(sentences) > 1 else ""
    body = sentences[1:-1] if len(sentences) > 2 else []
    keywords = [k.strip() for k in (keywords or []) if k.strip()]
    title = title.strip() or (hook[:70].rstrip(".!? ") if hook else "Untitled Short")
    if not keywords:
        keywords = [re.sub(r"[^\w ]", "", title).strip() or "abstract background"]
    lines = [{"text": s, "visual_keyword": keywords[i % len(keywords)]} for i, s in enumerate(body)]
    tags = [t.strip().lstrip("#") for t in (hashtags or []) if t.strip()]
    if "shorts" not in [t.lower() for t in tags]:
        tags.insert(0, "shorts")
    script: dict[str, Any] = {
        "title": title, "hook": hook, "lines": lines, "cta": cta, "visual_fallback": keywords[0],
        "thumbnail_text": " ".join(re.sub(r"[^\w' ]+", " ", title).split()[:6]),
        "description": description.strip() or f"{title}\n\n" + " ".join("#" + t for t in tags),
        "hashtags": tags, "full_text": text, "word_count": len(text.split()),
        "attempt": 1, "backend": "custom", "qa": None, "qa_problems": [],
    }
    try:
        script["qa"] = judge_script(script, qa_blueprint or {"format": "custom", "topic": "custom", "hook_style": "custom",
                                                            "why_it_works": "user-written script"})
    except Exception as exc:  # noqa: BLE001 - QA is advisory for user scripts
        log.warning("TypeSafe QA skipped for custom script: %s", exc)
    return finalize_metadata(script)


def _ollama_fallback_ready() -> bool:
    from . import ollama_backend
    return bool(ollama_backend.config()["fallback"]) and ollama_backend.available()


def pick_backend(preference: str = "auto") -> str:
    """'api' needs ANTHROPIC_API_KEY; 'claude_code' uses Claude Code headless on a Pro/Max subscription;
    'ollama' is a free local model. With 'auto' (or when Claude is missing) a ready Ollama model is the fallback."""
    from .config import has_anthropic
    from . import claude_code_backend
    if preference == "ollama":
        from . import ollama_backend
        if ollama_backend.available():
            return "ollama"
        raise RuntimeError(f"No script backend: Ollama is not running or the model {ollama_backend.config()['model']!r} "
                           "is not pulled (run `ollama pull <model>`).")
    if preference == "api" or (preference == "auto" and has_anthropic()):
        return "api"
    if preference in ("claude_code", "auto"):
        binary = claude_code_backend.find_claude_binary()
        if binary:
            return "claude_code"
        if _ollama_fallback_ready():
            log.warning("Claude Code was not found; writing with the free local model (Ollama) instead")
            return "ollama"
        from .config import env
        token_note = ("CLAUDE_CODE_OAUTH_TOKEN is set, but" if env("CLAUDE_CODE_OAUTH_TOKEN")
                      else "no CLAUDE_CODE_OAUTH_TOKEN is set and")
        raise RuntimeError(f"No script backend: {token_note} the Claude Code program (claude.exe) was not found on this "
                           "machine. Open the Claude desktop app once (it installs Claude Code), or set CLAUDE_CODE_BIN in "
                           "Settings to the full path of claude.exe, or install it with `npm install -g @anthropic-ai/claude-code`. "
                           "Alternatively set ANTHROPIC_API_KEY to use the API instead.")
    raise RuntimeError("No script backend: set ANTHROPIC_API_KEY, or run `claude setup-token` and set "
                       "CLAUDE_CODE_OAUTH_TOKEN (see README).")


def _draft(client, system: str, user_prompt: str, backend: str = "") -> ShortScript:
    if backend == "ollama":
        from . import ollama_backend
        return ShortScript.model_validate(ollama_backend.generate_json(system, user_prompt, ShortScript.model_json_schema()))
    if client is not None:
        response = client.messages.parse(model=MODEL, max_tokens=4000, system=system,
                                         messages=[{"role": "user", "content": user_prompt}], output_format=ShortScript)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"Claude declined to write this script: {getattr(details, 'explanation', '')}")
        return response.parsed_output
    from . import claude_code_backend
    data = claude_code_backend.generate_json(system, user_prompt, ShortScript.model_json_schema())
    return ShortScript.model_validate(data)


def _write_with_qa(*, system: str, user_prompt: str, blueprint: dict[str, Any], backend: str, max_attempts: int,
                   min_hook_score: float, original_text: str | None = None,
                   repeat_check: Any = None) -> dict[str, Any]:
    """Draft, have TypeSafe judge it, send the reviewer notes back, up to `max_attempts` times."""
    backend = pick_backend(backend)
    log.info("script backend: %s", backend)
    client = anthropic.Anthropic() if backend == "api" else None
    feedback = ""
    best: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            parsed = _draft(client, system, user_prompt + feedback, backend)
        except Exception as exc:  # noqa: BLE001
            # Claude unreachable, signed out or over its usage limit: finish the job with the free local model.
            if backend == "ollama" or not _ollama_fallback_ready():
                raise
            log.warning("Claude failed (%s); switching to the free local model (Ollama) for this script",
                        str(exc)[:160])
            backend, client = "ollama", None
            parsed = _draft(client, system, user_prompt + feedback, backend)
        script = parsed.model_dump()
        script["backend"] = backend
        script["full_text"] = " ".join([script["hook"], *[ln["text"] for ln in script["lines"]], script["cta"]])
        script["word_count"] = len(script["full_text"].split())
        script["attempt"] = attempt
        if original_text is not None:
            script["original_text"] = original_text

        repeated = repeat_check(script) if repeat_check else None
        script["repeat_of"] = repeated
        if repeated:
            log.info("script attempt %d rejected: same subject as the existing video %r", attempt, repeated)
            taken = (f"\n\nREJECTED: that draft ({script['title']!r}) repeats a video already made: {repeated!r}. "
                     "Pick a completely different subject (a different story, fact, person or experiment) and write it again.")
            user_prompt += taken
            continue
        qa = judge_script(script, blueprint, original_text=original_text)
        script["qa"] = qa
        script["qa_problems"] = []
        if qa is None:
            return finalize_metadata(script)  # no TypeSafe key: accept as written
        problems = []
        hook = qa["hook_strength"]["score"]
        if hook < min_hook_score:
            problems.append(f"the hook scored {hook:.1f}/3 for scroll-stopping power; make it more specific and surprising")
        if qa["policy_risk"]["noul"] > 0.5:
            problems.append("the script risks violating YouTube policy; remove any risky claim or instruction")
        if original_text is None and qa["matches_format"]["noul"] < 0.5:
            problems.append(f"it drifted away from the {blueprint['format']} format / {blueprint['hook_style']} hook")
        if qa["has_payoff"]["noul"] < 0.5:
            problems.append("it never pays off what the hook promised; end with the answer")
        if qa["clarity"]["score"] < 1.0:
            problems.append("sentences are too long or jumpy for a fast voiceover")
        if original_text is not None and qa.get("faithful", {}).get("noul", 1.0) < 0.6:
            problems.append("it changed or invented facts, or lost the point of the original; keep every claim from the original")
        if len(script["title"]) > TITLE_MAX:
            problems.append(f"the title is {len(script['title'])} characters; YouTube allows 100 and shows about 70, so make it shorter")
        script["qa_problems"] = problems
        if best is None or len(problems) < len(best["qa_problems"]):
            best = script
        if not problems:
            return finalize_metadata(script)
        log.info("script attempt %d rejected: %s", attempt, "; ".join(problems))
        feedback = "\n\nA reviewer rejected the previous draft because: " + "; ".join(problems) + ". Rewrite it."
    if best is None:
        # Every draft repeated an existing video: better no Short than a duplicate on the channel.
        raise RuntimeError(f"all {max_attempts} drafts repeated subjects that were already made; nothing produced")
    return finalize_metadata(best)


def generate_script(blueprint: dict[str, Any], *, target_seconds: int = 40, angle: str | None = None,
                    max_attempts: int = 3, min_hook_score: float = 2.0, backend: str = "auto",
                    avoid_titles: list[str] | None = None, check_repeats: bool = True) -> dict[str, Any]:
    from . import history

    known = history.known_videos() if check_repeats else []
    if avoid_titles is None:
        avoid_titles = history.avoid_titles(blueprint)
    return _write_with_qa(system=SYSTEM, user_prompt=_prompt(blueprint, target_seconds, angle, avoid_titles),
                          blueprint=blueprint, backend=backend, max_attempts=max(max_attempts, 4 if check_repeats else 0),
                          min_hook_score=min_hook_score,
                          repeat_check=(lambda s: history.find_repeat(s, known)) if check_repeats else None)


ENHANCE_SYSTEM = SYSTEM + """
You are now EDITING a script the creator wrote themselves. Keep every fact, number, claim and the creator's
point exactly; you may reorder, cut filler, tighten wording and add connective phrasing so it flows when spoken.
Never invent facts or examples that are not in the original. Add a scroll-stopping hook as the first sentence
(built from the most surprising idea already in the script), make sure the ending pays off the hook, and finish
with a short call to action. Keep the creator's voice."""


def enhance_script(text: str, *, title: str = "", description: str = "", hashtags: list[str] | None = None,
                   keywords: list[str] | None = None, target_seconds: int | None = None, max_attempts: int = 3,
                   min_hook_score: float = 2.0, backend: str = "auto") -> dict[str, Any]:
    """Rewrite a user script into a stronger Short without changing its substance."""
    text = re.sub(r"\s+", " ", text.replace("\r", "\n")).strip()
    if not text:
        raise ValueError("The script is empty.")
    words = len(text.split())
    target = target_seconds or max(20, min(60, round(words / 2.6)))
    hints = []
    if title.strip():
        hints.append(f"Preferred title (keep or improve slightly): {title.strip()}")
    if hashtags:
        hints.append("Include these hashtags: " + ", ".join(h.strip().lstrip("#") for h in hashtags if h.strip()))
    if keywords:
        hints.append("Preferred stock-footage search terms to use for visual_keyword where they fit: " + ", ".join(keywords))
    if description.strip():
        hints.append(f"Base the description on this: {description.strip()}")
    user_prompt = (f"Original script written by the creator:\n\"\"\"\n{text}\n\"\"\"\n\n"
                   f"Target length: about {target} seconds ({int(target * 2.6)} spoken words); the original is {words} words.\n"
                   + ("\n".join(hints) + "\n" if hints else "")
                   + "Rewrite it now as the JSON script.")
    blueprint = {"format": "creator script", "topic": "creator's own subject", "hook_style": "strongest idea first",
                 "why_it_works": "the creator's own material, edited for a stronger open, flow and payoff"}
    return _write_with_qa(system=ENHANCE_SYSTEM, user_prompt=user_prompt, blueprint=blueprint, backend=backend,
                          max_attempts=max_attempts, min_hook_score=min_hook_score, original_text=text)
