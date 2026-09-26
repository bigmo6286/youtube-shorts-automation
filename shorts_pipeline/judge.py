"""TypeSafe (System One / Jev) judgments.

Every judgment is one narrow question. Code owns the weights, thresholds and workflow
(see rank.py / analyze.py); Jev supplies the semantic reads that plain code cannot make.
All questions about one Short go in ONE request: they run in parallel and cost only tokens.
"""
from __future__ import annotations

import logging
from typing import Any

from .config import has_typesafe
from .storage import JsonCache

log = logging.getLogger(__name__)

FORMATS = {
    "listicle_facts": "Rapid facts, tips or a numbered list read over footage or images",
    "storytime": "A narrated story or anecdote with a beginning, tension and payoff",
    "explainer": "Explains how or why something works; educational tone",
    "tutorial_howto": "Step-by-step instructions to do or make something",
    "motivational": "Motivational or inspirational message, quote or speech",
    "comedy_skit": "Scripted joke, skit or meme performed on camera",
    "reaction_commentary": "A person reacting to or commenting on a clip, news or post",
    "talking_head_opinion": "One person speaking directly to camera giving an opinion or advice",
    "gameplay_clip": "Video game footage, highlights or streamer moments",
    "sports_highlight": "Real sports play, highlight or athlete moment",
    "music_dance": "Music performance, lip-sync or dance",
    "satisfying_visual": "Satisfying, ASMR, art process or oddly-satisfying visuals with little speech",
    "animal_cute": "Pets or animals doing something cute or funny",
    "product_review": "Reviewing, unboxing or demonstrating a product",
    "news_current_events": "Reporting or summarising a current news event",
    "other": "None of the above fits",
}

TOPICS = {
    "money_finance": "Money, investing, side hustles, business, wealth",
    "tech_ai": "Technology, gadgets, software, AI",
    "science_nature": "Science, space, nature, how the world works",
    "history": "Historical events, people or eras",
    "psychology_relationships": "Human behaviour, dating, relationships, social dynamics",
    "health_fitness": "Health, diet, fitness, body",
    "self_improvement": "Productivity, discipline, habits, mindset",
    "entertainment_celebrity": "Movies, TV, celebrities, pop culture",
    "gaming": "Video games",
    "sports": "Sports and athletes",
    "food": "Food, cooking, restaurants",
    "travel_places": "Travel, places, geography",
    "animals": "Animals and pets",
    "comedy_general": "Comedy with no specific subject",
    "true_crime_mystery": "Crime, mysteries, unsolved cases, creepy stories",
    "news_politics": "News, politics, society",
    "other": "None of the above fits",
}

HOOKS = {
    "question": "Opens by asking the viewer a question",
    "bold_claim": "Opens with a surprising or contrarian statement",
    "curiosity_gap": "Teases something the viewer must keep watching to learn (wait for it, number 3 will shock you)",
    "direct_address": "Opens by calling out the viewer (if you are X, stop scrolling)",
    "story_open": "Drops straight into a story scene or moment",
    "visual_only": "The hook is a visual or action, not words",
    "no_hook": "Starts with an intro, greeting or context with no hook",
}


def _q(kind: str, instructions: Any, criteria: Any = None) -> dict[str, Any]:
    q: dict[str, Any] = {"type": kind, "instructions": instructions}
    if criteria is not None:
        q["criteria"] = criteria
    return q


SHORT_QUESTIONS: dict[str, dict[str, Any]] = {
    "format": _q("choice", {
        "question": "Which content format is this Short?",
        "focus": "Judge the delivery format, not the subject matter.",
    }, FORMATS),
    "topic": _q("choice", "Which subject area is this Short mainly about?", TOPICS),
    "hook_style": _q("choice", {
        "question": "How does this Short try to grab attention in its first seconds?",
        "evidence": "Use the opening of `transcript` when present, otherwise `title` and `description`.",
    }, HOOKS),
    "hook_strength": _q("score", {
        "question": "How strong is the opening hook at stopping a viewer from scrolling?",
        "evidence": "Judge the first one or two sentences of `transcript`, or the title if there is no transcript.",
    }, [
        {"level": "No hook: generic intro, greeting or slow setup", "examples": ["hey guys welcome back", "so today we are going to"]},
        {"level": "Weak hook: states the subject but gives no reason to keep watching", "examples": ["here are some facts about space"]},
        {"level": "Good hook: creates a specific question or promise", "examples": ["most people get this wrong about sleep"]},
        {"level": "Strong hook: surprising, specific and impossible to leave unanswered", "examples": ["a man was legally dead for 3 years and still voted"]},
    ]),
    "replicable": _q("score", {
        "question": "Could a solo creator remake this style of Short with only an AI voiceover, stock footage or images, and on-screen captions, with no camera, no face and no original footage?",
        "note": "Judge the format, not this exact video. A story or fact list is replicable; a specific dance, prank or sports play is not.",
    }, [
        "Impossible: the value is a specific person, live event, original footage or performance",
        "Hard: would need original visuals or a person on camera to work",
        "Feasible: works with voiceover plus stock footage, though weaker without the original",
        "Easy: the format is fundamentally narration or text over generic visuals",
    ]),
    "evergreen": _q("score", "How long will this Short stay worth watching?", [
        "Days: tied to a news event, a trend or a date",
        "Weeks: about something current but not breaking",
        "Months: broadly interesting, only loosely time-bound",
        "Years: timeless subject that will interest viewers indefinitely",
    ]),
    "english_ok": _q("noul", "Would a general English-speaking audience understand and enjoy this Short without knowing another language or a niche in-group?", {
        "true": "Content is in English or needs no language to enjoy, and the subject is broadly accessible",
        "false": "Content is mainly in another language, or depends on a niche fandom, local context or in-joke",
    }),
    "is_promo": _q("noul", "Is this Short primarily an advertisement, sponsored promotion, product sale, or self-promotion of a channel or service?", {
        "true": "Selling, promoting a product, course, app, giveaway or asking to follow or buy",
        "false": "Content for its own sake; a small end-card call to action does not count",
    }),
    "clickbait_risk": _q("noul", "Does the title or hook promise something the content is unlikely to deliver, or rely on outrage or misleading claims?", None),
}

SCORE_LEVELS = {"hook_strength": 4, "replicable": 4, "evergreen": 4, "clarity": 3}

SCRIPT_QA_QUESTIONS: dict[str, dict[str, Any]] = {
    "hook_strength": SHORT_QUESTIONS["hook_strength"],
    "clarity": _q("score", "How easy is this script to follow when heard once as a fast voiceover?", [
        "Confusing: jumps around or uses jargon and long sentences",
        "Followable with effort",
        "Clear and punchy: short sentences, one idea at a time",
    ]),
    "matches_format": _q("noul", "Does the `script` actually follow the `blueprint` (its format, topic and hook style)?", None),
    "policy_risk": _q("noul", "Could this script violate YouTube policy or be flagged: medical or financial claims stated as fact, hate, harassment, dangerous acts, sexual content, or misinformation?", None),
    "has_payoff": _q("noul", "Does the script deliver a satisfying payoff or answer to what the hook promised, rather than trailing off?", None),
}


def _client():
    from typesafe_sdk import TypeSafeClient
    return TypeSafeClient(timeout=120.0)


def _answers_to_plain(response) -> dict[str, Any]:
    out: dict[str, Any] = {"model": getattr(response, "model", None)}
    for qid, ans in response.answers.items():
        kind = ans.type
        if kind == "choice":
            out[qid] = {"type": "choice", "choice": ans.choice, "confidence": ans.confidence,
                        "probabilities": dict(ans.probabilities)}
        elif kind == "score":
            out[qid] = {"type": "score", "score": ans.score, "confidence": ans.confidence,
                        "probabilities": {str(k): v for k, v in ans.probabilities.items()}}
        else:
            out[qid] = {"type": "noul", "noul": ans.noul}
    return out


def short_state(meta: dict[str, Any]) -> dict[str, Any]:
    """The evidence Jev gets about one Short. Named fields, nothing the questions do not need."""
    return {
        "title": meta.get("title", ""),
        "description": (meta.get("description") or "")[:500],
        "hashtags_and_tags": meta.get("tags") or [],
        "youtube_category": meta.get("categories") or [],
        "channel": meta.get("channel", ""),
        "duration_seconds": meta.get("duration"),
        "declared_language": meta.get("language"),
        "transcript": meta.get("transcript") or "(no transcript available)",
    }


_JUDGE_CACHE = JsonCache("judgments")


def judge_short(meta: dict[str, Any], *, use_cache: bool = True) -> dict[str, Any] | None:
    """One TypeSafe request per Short answering every question at once."""
    if not has_typesafe():
        return None
    key = meta["id"] + ("+t" if meta.get("transcript") else "")
    if use_cache:
        cached = _JUDGE_CACHE.get(key)
        if cached:
            return cached
    with _client() as client:
        response = client.system_one(state=short_state(meta), questions=SHORT_QUESTIONS)
    plain = _answers_to_plain(response)
    _JUDGE_CACHE.set(key, plain)
    return plain


def judge_script(script: dict[str, Any], blueprint: dict[str, Any]) -> dict[str, Any] | None:
    if not has_typesafe():
        return None
    state = {
        "blueprint": {k: blueprint.get(k) for k in ("format", "topic", "hook_style", "why_it_works")},
        "script": {k: script.get(k) for k in ("title", "hook", "lines", "cta")},
        "transcript": script.get("full_text", ""),  # the shared hook_strength question reads `transcript`
        "title": script.get("title", ""),
    }
    with _client() as client:
        response = client.system_one(state=state, questions=SCRIPT_QA_QUESTIONS)
    return _answers_to_plain(response)


def normalized_score(judgment: dict[str, Any] | None, qid: str, default: float = 0.5) -> float:
    """Score on 0..1 by dividing by the top level number."""
    if not judgment or qid not in judgment:
        return default
    levels = SCORE_LEVELS[qid]
    return float(judgment[qid]["score"]) / (levels - 1)
