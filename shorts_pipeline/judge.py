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

# Bump when FORMATS / TOPICS / HOOKS change so cached judgments are redone with the new labels.
TAXONOMY_VERSION = "v2"

FORMATS = {
    # narration-led (faceless-friendly)
    "single_fact": "One surprising fact or statistic, explained in a few sentences",
    "listicle_facts": "Several rapid facts, tips or items in a numbered or implied list",
    "explainer": "Explains how or why something works; educational tone",
    "myth_busting": "States a common belief and shows why it is wrong",
    "storytime": "A narrated story or anecdote with a beginning, tension and payoff",
    "history_story": "A dramatic story from history, told as a narrative",
    "true_crime_story": "A narrated real crime, case or disappearance",
    "mystery_unsolved": "An unexplained event, artifact or phenomenon presented as a mystery",
    "horror_scary_story": "A creepy or scary story told for chills",
    "reddit_confession_story": "A first-person confession or drama story, often from Reddit-style posts",
    "quote_wisdom": "A quote, aphorism or short piece of wisdom with brief commentary",
    "motivational": "Motivational or inspirational message or speech",
    "life_hack_tip": "A practical trick, hack or tip to do something better",
    "psychology_trick": "A psychological effect, bias or persuasion trick explained",
    "quiz_riddle": "Asks the viewer a question, riddle or test and reveals the answer",
    "ranking_top_list": "Ranks things from worst to best, or counts down a top list",
    "comparison_versus": "Compares two or more things side by side (X vs Y)",
    "then_vs_now": "How something changed over time, before and after",
    "movie_tv_recap": "Recaps or explains a film, show, scene or plot",
    "tutorial_howto": "Step-by-step instructions to do or make something",
    "recipe_cooking": "Preparing a dish, step by step",
    "product_review": "Reviewing, unboxing or demonstrating a product",
    "news_current_events": "Reporting or summarising a current news event",
    "conspiracy_speculation": "Speculative or conspiracy-style theory presented for intrigue",
    # performance-led (needs a person, original footage or a game)
    "comedy_skit": "Scripted joke, skit or meme performed on camera",
    "reaction_commentary": "A person reacting to or commenting on a clip, news or post",
    "talking_head_opinion": "One person speaking directly to camera giving an opinion or advice",
    "vlog_day_in_life": "Personal vlog or day-in-the-life footage",
    "prank_challenge": "Prank, dare or challenge performed on camera",
    "gameplay_clip": "Video game footage, highlights or streamer moments",
    "sports_highlight": "Real sports play, highlight or athlete moment",
    "music_dance": "Music performance, lip-sync or dance",
    "satisfying_visual": "Satisfying, ASMR, art process or oddly-satisfying visuals with little speech",
    "animal_cute": "Pets or animals doing something cute or funny on camera",
    "other": "None of the above fits",
}

TOPICS = {
    "money_finance": "Personal finance, saving, debt, wealth habits",
    "investing_crypto": "Stocks, investing, crypto, markets",
    "business_entrepreneurship": "Business, startups, marketing, entrepreneurs",
    "side_hustles_online_income": "Side hustles, freelancing, making money online",
    "tech_gadgets": "Consumer technology, phones, gadgets, software",
    "ai_future": "Artificial intelligence, robots, the future",
    "programming_coding": "Programming, developers, software engineering",
    "science_general": "Science and how the world works",
    "space_astronomy": "Space, planets, astronomy, rockets",
    "biology_human_body": "The human body, biology, medicine facts",
    "physics_chemistry": "Physics, chemistry, engineering",
    "nature_environment": "Nature, weather, climate, oceans, environment",
    "animals_wildlife": "Wild animals and creatures",
    "pets": "Dogs, cats and other pets",
    "history_ancient": "Ancient and medieval history",
    "history_modern": "Modern history, 1800s to today",
    "war_military": "Wars, battles, military",
    "geography_countries": "Countries, cities, maps, cultures of the world",
    "psychology_mind": "Psychology, the mind, behaviour, mental tricks",
    "relationships_dating": "Dating, relationships, marriage, friendship",
    "family_parenting": "Parents, children, family life",
    "self_improvement": "Habits, discipline, productivity, mindset",
    "motivation_success": "Motivation, success, ambition, achievement stories",
    "philosophy_stoicism": "Philosophy, stoicism, meaning of life",
    "health_medical": "Health, illness, doctors, medical facts",
    "fitness_gym": "Exercise, gym, training, sports science",
    "nutrition_diet": "Food science, diets, nutrition",
    "cooking_recipes": "Cooking, recipes, restaurants",
    "travel_places": "Travel, destinations, places to visit",
    "cars_vehicles": "Cars, motorcycles, planes, vehicles",
    "true_crime": "Real crimes, criminals, cases",
    "mysteries_paranormal": "Unsolved mysteries, paranormal, unexplained events",
    "movies_tv": "Films, series, streaming, characters",
    "celebrities_pop_culture": "Celebrities, fame, pop culture, gossip",
    "music": "Music, artists, songs",
    "gaming": "Video games",
    "sports": "Sports and athletes",
    "football_soccer": "Football (soccer), clubs, players",
    "comedy_general": "Comedy with no specific subject",
    "memes_internet_culture": "Memes, internet trends, creators",
    "education_general_knowledge": "General knowledge, school subjects, trivia",
    "law_rights": "Law, legal rights, courts",
    "religion_spirituality": "Religion, faith, spirituality",
    "art_design": "Art, design, architecture, creativity",
    "fashion_beauty": "Fashion, style, beauty, skincare",
    "home_diy": "Home, DIY, cleaning, organisation",
    "language_words": "Languages, words, etymology, grammar",
    "news_politics": "News, politics, society",
    "other": "None of the above fits",
}

# old labels (cached judgments, produced videos, channel feedback) mapped onto the new taxonomy
LEGACY_TOPICS = {"tech_ai": "tech_gadgets", "science_nature": "science_general", "history": "history_modern",
                 "psychology_relationships": "psychology_mind", "health_fitness": "health_medical",
                 "entertainment_celebrity": "celebrities_pop_culture", "food": "cooking_recipes", "animals": "animals_wildlife",
                 "true_crime_mystery": "true_crime"}


def canonical_topic(topic: str) -> str:
    return LEGACY_TOPICS.get(topic, topic)

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
        "focus": "Judge the delivery format, not the subject matter. Pick the most specific option that fits; "
                 "use the broader one (storytime, explainer, listicle_facts) only when no specific one does.",
    }, FORMATS),
    "topic": _q("choice", {
        "question": "Which subject area is this Short mainly about?",
        "focus": "Pick the most specific option; 'other' only when nothing fits at all.",
    }, TOPICS),
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

ENHANCE_QA_QUESTIONS: dict[str, dict[str, Any]] = {
    "faithful": _q("noul", {
        "question": "Does the rewritten `script` keep every fact, claim and the core message of `original_script`, adding nothing untrue?",
        "note": "Rewording, reordering, cutting filler and adding a hook or call to action are fine. Inventing facts, changing numbers or shifting the point is not.",
    }, {
        "true": "Same facts and message, only the delivery changed",
        "false": "New or changed claims, or the point of the original was lost",
    }),
}

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
    key = meta["id"] + ("+t" if meta.get("transcript") else "") + "+" + TAXONOMY_VERSION
    if use_cache:
        cached = _JUDGE_CACHE.get(key)
        if cached:
            return cached
    with _client() as client:
        response = client.system_one(state=short_state(meta), questions=SHORT_QUESTIONS)
    plain = _answers_to_plain(response)
    _JUDGE_CACHE.set(key, plain)
    return plain


LOOP_QUESTION = _q("noul", (
    "Shorts replay automatically. Does the final spoken line (`script.cta`) lead straight back into the opening line "
    "(`script.hook`), so that when the video restarts the two read as one continuous sentence or thought, with no "
    "closing question, 'follow for more' or sign-off in between?"), None)


def judge_script(script: dict[str, Any], blueprint: dict[str, Any], original_text: str | None = None,
                 loop: bool = False) -> dict[str, Any] | None:
    if not has_typesafe():
        return None
    state = {
        "blueprint": {k: blueprint.get(k) for k in ("format", "topic", "hook_style", "why_it_works")},
        "script": {k: script.get(k) for k in ("title", "hook", "lines", "cta")},
        "transcript": script.get("full_text", ""),  # the shared hook_strength question reads `transcript`
        "title": script.get("title", ""),
    }
    questions = dict(SCRIPT_QA_QUESTIONS)
    if loop:
        questions["loops"] = LOOP_QUESTION
    if original_text:
        state["original_script"] = original_text
        questions.update(ENHANCE_QA_QUESTIONS)
    with _client() as client:
        response = client.system_one(state=state, questions=questions)
    return _answers_to_plain(response)


def normalized_score(judgment: dict[str, Any] | None, qid: str, default: float = 0.5) -> float:
    """Score on 0..1 by dividing by the top level number."""
    if not judgment or qid not in judgment:
        return default
    levels = SCORE_LEVELS[qid]
    return float(judgment[qid]["score"]) / (levels - 1)
