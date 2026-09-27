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
    visual_keyword: str = Field(description="2-4 word stock-footage search term that matches this line")


class ShortScript(BaseModel):
    title: str = Field(description="YouTube title, under 70 chars, includes the hook idea, no clickbait lies")
    hook: str = Field(description="The first spoken sentence. Must stop the scroll within 2 seconds.")
    lines: list[ScriptLine] = Field(description="The body, in order, after the hook")
    cta: str = Field(description="One short closing line (question to the viewer or a soft follow ask)")
    description: str = Field(description="YouTube description, 1-3 sentences plus hashtags")
    hashtags: list[str] = Field(description="3-6 hashtags without the # sign, first one shorts")


SYSTEM = """You write scripts for faceless YouTube Shorts: an AI voice reads the script over stock footage with
big on-screen captions. Rules:
- Total spoken length must fit the target seconds at about 2.6 words per second.
- Every line is one short sentence. No "number one, number two" unless the format is a listicle.
- The hook is the first sentence and must be specific and surprising, in the requested hook style.
- Everything factual must be true and verifiable; if unsure, say "reportedly" or choose a different fact.
- No medical, legal or financial advice stated as fact. No hate, harassment, or dangerous instructions.
- Do not copy the exemplar Shorts. Use them only to understand the pacing and format that works.
- Write for speech: contractions, plain words, no emojis, no markdown."""


def _prompt(blueprint: dict[str, Any], target_seconds: int, angle: str | None) -> str:
    words = int(target_seconds * 2.6)
    exemplars = "\n".join(
        f"- Title: {e['title']!r}; opening transcript: {e.get('transcript', '')[:250]!r}"
        for e in blueprint.get("exemplars", [])
    ) or "- (none)"
    angle_line = f"Specific angle or subject to use: {angle}\n" if angle else "Pick a fresh, specific subject inside the topic.\n"
    return (
        f"Format: {blueprint['format']}\nTopic: {blueprint['topic']}\nHook style: {blueprint['hook_style']}\n"
        f"Why this works right now: {blueprint.get('why_it_works', '')}\n"
        f"{angle_line}"
        f"Target length: about {target_seconds} seconds, roughly {words} spoken words in total.\n\n"
        f"Trending exemplars (for pacing only, do not copy):\n{exemplars}\n\n"
        "Write the script now."
    )


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


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
        "title": title, "hook": hook, "lines": lines, "cta": cta,
        "description": description.strip() or f"{title}\n\n" + " ".join("#" + t for t in tags),
        "hashtags": tags, "full_text": text, "word_count": len(text.split()),
        "attempt": 1, "backend": "custom", "qa": None, "qa_problems": [],
    }
    try:
        script["qa"] = judge_script(script, qa_blueprint or {"format": "custom", "topic": "custom", "hook_style": "custom",
                                                            "why_it_works": "user-written script"})
    except Exception as exc:  # noqa: BLE001 - QA is advisory for user scripts
        log.warning("TypeSafe QA skipped for custom script: %s", exc)
    return script


def _draft_via_api(client: "anthropic.Anthropic", user_prompt: str) -> ShortScript:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        output_format=ShortScript,
    )
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise RuntimeError(f"Claude declined to write this script: {getattr(details, 'explanation', '')}")
    return response.parsed_output


def _draft_via_claude_code(user_prompt: str) -> ShortScript:
    from . import claude_code_backend
    data = claude_code_backend.generate_json(SYSTEM, user_prompt, ShortScript.model_json_schema())
    return ShortScript.model_validate(data)


def pick_backend(preference: str = "auto") -> str:
    """'api' needs ANTHROPIC_API_KEY; 'claude_code' uses Claude Code headless on a Pro/Max subscription."""
    from .config import has_anthropic
    from . import claude_code_backend
    if preference == "api" or (preference == "auto" and has_anthropic()):
        return "api"
    if preference in ("claude_code", "auto") and claude_code_backend.find_claude_binary():
        return "claude_code"
    raise RuntimeError("No script backend: set ANTHROPIC_API_KEY, or run `claude setup-token` and set "
                       "CLAUDE_CODE_OAUTH_TOKEN (see README).")


def generate_script(blueprint: dict[str, Any], *, target_seconds: int = 40, angle: str | None = None,
                    max_attempts: int = 3, min_hook_score: float = 2.0, backend: str = "auto") -> dict[str, Any]:
    backend = pick_backend(backend)
    log.info("script backend: %s", backend)
    client = anthropic.Anthropic() if backend == "api" else None
    feedback = ""
    best: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        user_prompt = _prompt(blueprint, target_seconds, angle) + feedback
        parsed = _draft_via_api(client, user_prompt) if client else _draft_via_claude_code(user_prompt)
        script = parsed.model_dump()
        script["backend"] = backend
        script["full_text"] = " ".join([script["hook"], *[ln["text"] for ln in script["lines"]], script["cta"]])
        script["word_count"] = len(script["full_text"].split())
        script["attempt"] = attempt

        qa = judge_script(script, blueprint)
        script["qa"] = qa
        script["qa_problems"] = []
        if qa is None:
            return script  # no TypeSafe key: accept as written
        problems = []
        hook = qa["hook_strength"]["score"]
        if hook < min_hook_score:
            problems.append(f"the hook scored {hook:.1f}/3 for scroll-stopping power; make it more specific and surprising")
        if qa["policy_risk"]["noul"] > 0.5:
            problems.append("the script risks violating YouTube policy; remove any risky claim or instruction")
        if qa["matches_format"]["noul"] < 0.5:
            problems.append(f"it drifted away from the {blueprint['format']} format / {blueprint['hook_style']} hook")
        if qa["has_payoff"]["noul"] < 0.5:
            problems.append("it never pays off what the hook promised; end with the answer")
        if qa["clarity"]["score"] < 1.0:
            problems.append("sentences are too long or jumpy for a fast voiceover")
        script["qa_problems"] = problems
        if best is None or len(problems) < len(best["qa_problems"]):
            best = script
        if not problems:
            return script
        log.info("script attempt %d rejected: %s", attempt, "; ".join(problems))
        feedback = "\n\nA reviewer rejected the previous draft because: " + "; ".join(problems) + ". Rewrite it."
    assert best is not None
    return best
