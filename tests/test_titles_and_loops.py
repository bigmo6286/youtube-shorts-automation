"""Search-driven titles (seeds, suggestions, TypeSafe pick rules) and loop-friendly Shorts (prompt, QA, hook overlay)."""
from types import SimpleNamespace

import pytest

from shorts_pipeline import captions, judge, script_gen, titles

SCRIPT = {"title": "Every Time an Octopus Swims, One of Its Hearts Stops", "hook": "An octopus has three hearts.",
          "visual_fallback": "octopus swimming underwater", "thumbnail_text": "ONE HEART STOPS",
          "full_text": "An octopus has three hearts. One stops whenever it swims."}


def test_seed_queries_are_two_word_subject_phrases():
    seeds = titles.seed_queries(SCRIPT)
    assert all(len(s.split()) == 2 for s in seeds) and len(seeds) <= 4
    assert "octopus swims" in seeds and not any("swimming" in s for s in seeds)    # title words, not the footage term
    assert not any(s.startswith("time ") for s in seeds)                            # generic words are not subjects


def test_seeds_ignore_the_stock_footage_term():
    """Regression: 'vintage marathon race' (a footage search term) once made every seed start with 'vintage'."""
    marathon = {"title": "The Slowest Marathon in History Took 54 Years to Finish", "thumbnail_text": "54 YEAR MARATHON",
                "hook": "The slowest marathon in history took fifty-four years to finish.",
                "full_text": "The slowest marathon in history took fifty-four years to finish.",
                "visual_fallback": "vintage marathon race"}
    assert titles.subject_words(marathon, 2) == ["marathon", "slowest"]
    seeds = titles.seed_queries(marathon)
    assert seeds[0] == "slowest marathon" and not any("vintage" in s for s in seeds)


def test_suggestions_off_subject_are_dropped(monkeypatch):
    monkeypatch.setattr(titles, "suggestions", lambda q, cfg=None: ["octopus heartbeat", "vintage history game", "octopus"])
    assert titles.search_phrases(SCRIPT) == ["octopus heartbeat"]


def test_suggestions_parse_youtube_format(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return ["octopus heart", ["octopus heart", "octopus heartbeat", "octopus hearts and brains"], [], {}]
    monkeypatch.setattr(titles.requests, "get", lambda *a, **k: _Resp())
    assert titles.suggestions("octopus heart") == ["octopus heart", "octopus heartbeat", "octopus hearts and brains"]


def _fake_typesafe(monkeypatch, tap, accurate):
    letters = "ABCD"

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def system_one(self, state, questions):
            answers = {"best": SimpleNamespace(probabilities={letters[i]: p for i, p in enumerate(tap)})}
            for i, a in enumerate(accurate):
                answers[f"accurate_{i}"] = SimpleNamespace(noul=a)
            return SimpleNamespace(answers=answers)
    monkeypatch.setattr(judge, "has_typesafe", lambda: True)
    monkeypatch.setattr(judge, "_client", lambda: _Client())


def test_pick_prefers_an_accurate_clearly_better_variant(monkeypatch):
    _fake_typesafe(monkeypatch, tap=[0.2, 0.6, 0.2], accurate=[0.9, 0.9, 0.9])
    assert titles.pick(SCRIPT, ["orig", "v1", "v2"], ["octopus heart"])[0] == 1


def test_pick_rejects_an_inaccurate_variant_and_coin_flips(monkeypatch):
    _fake_typesafe(monkeypatch, tap=[0.2, 0.7, 0.1], accurate=[0.9, 0.3, 0.9])     # favourite is misleading
    assert titles.pick(SCRIPT, ["orig", "v1", "v2"], [])[0] == 0
    _fake_typesafe(monkeypatch, tap=[0.48, 0.52], accurate=[0.9, 0.9])             # not clearly better
    assert titles.pick(SCRIPT, ["orig", "v1"], [])[0] == 0


def test_improve_keeps_the_title_when_anything_fails(monkeypatch):
    monkeypatch.setattr(titles, "search_phrases", lambda s: (_ for _ in ()).throw(RuntimeError("offline")))
    s = dict(SCRIPT)
    assert titles.improve(s)["title"] == SCRIPT["title"]


# ------------------------------------------------------------------ loops
def test_prompt_asks_for_a_loop_ending(monkeypatch):
    monkeypatch.setattr(script_gen, "loop_endings", lambda: True)
    p = script_gen._prompt({"format": "single_fact", "topic": "animals_wildlife", "hook_style": "bold_claim"}, 40, None, [])
    assert "LOOP ENDING" in p and "No closing question" in p
    monkeypatch.setattr(script_gen, "loop_endings", lambda: False)
    assert "LOOP ENDING" not in script_gen._prompt({"format": "single_fact", "topic": "x", "hook_style": "y"}, 40, None, [])


def test_loop_question_is_asked_only_in_loop_mode(monkeypatch):
    seen = {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def system_one(self, state, questions):
            seen["q"] = set(questions)
            return SimpleNamespace(answers={}, model="x")
    monkeypatch.setattr(judge, "has_typesafe", lambda: True)
    monkeypatch.setattr(judge, "_client", lambda: _Client())
    judge.judge_script({"title": "t", "hook": "h", "lines": [], "cta": "c", "full_text": "h c"}, {"format": "f"}, loop=True)
    assert "loops" in seen["q"]
    judge.judge_script({"title": "t", "hook": "h", "lines": [], "cta": "c", "full_text": "h c"}, {"format": "f"})
    assert "loops" not in seen["q"]


def test_hook_overlay_is_added_on_top_from_frame_one(tmp_path):
    words = [{"text": w, "start": i * 0.4, "end": i * 0.4 + 0.35} for i, w in enumerate("an octopus has three hearts".split())]
    ass = captions.write_ass(words, tmp_path / "c.ass", full_text="An octopus has three hearts.")
    captions.add_hook_overlay(ass, "one heart stops when it swims", 2.5)
    text = ass.read_text(encoding="utf-8")
    assert "Style: HookTitle" in text
    event = [l for l in text.splitlines() if ",HookTitle," in l][0]
    assert event.startswith("Dialogue: 1,0:00:00.00,0:00:02.50,HookTitle")
    assert "ONE HEART STOPS" in event and "\\N" in event                      # upper case, wrapped onto two lines


# ------------------------------------------------------------------ long videos (not Shorts)
def test_long_video_frame_is_landscape_and_resets():
    from shorts_pipeline import frame
    frame.use("landscape", long=True)
    assert frame.size() == (1920, 1080) and frame.landscape() and frame.long_video()
    frame.use()
    assert frame.size() == (1080, 1920) and not frame.long_video()


def test_long_script_is_never_cut_down(monkeypatch):
    from shorts_pipeline import script_gen
    original = " ".join(f"Sentence number {i} explains one more detail of the story." for i in range(60))   # 540 words
    short = {"title": "T", "hook": "H.", "lines": [{"text": "Only a summary.", "visual_keyword": "x"}], "cta": "C.",
             "description": "D #shorts", "hashtags": ["shorts", "history"], "visual_fallback": "x", "thumbnail_text": "X"}
    full = dict(short, lines=[{"text": f"Sentence number {i} explains one more detail of the story.", "visual_keyword": "x"}
                              for i in range(60)])
    drafts = iter([short, full])
    monkeypatch.setattr(script_gen, "pick_backend", lambda pref: "claude_code")
    monkeypatch.setattr(script_gen, "_draft", lambda *a, **k: script_gen.ShortScript.model_validate(next(drafts)))
    qa = {"hook_strength": {"score": 3}, "clarity": {"score": 2}, "policy_risk": {"noul": 0}, "matches_format": {"noul": 1},
          "has_payoff": {"noul": 1}, "faithful": {"noul": 1}}
    monkeypatch.setattr(script_gen, "judge_script", lambda *a, **k: qa)
    s = script_gen.enhance_script(original, long=True, max_attempts=3)
    assert s["word_count"] >= 500 and s["attempt"] == 2 and s["long_video"]
    assert "shorts" not in s["hashtags"] and "#shorts" not in s["description"].lower()


def test_as_written_script_gets_one_footage_term_per_sentence(monkeypatch):
    from shorts_pipeline import script_gen
    script = script_gen.custom_script("Owls turn their heads. They have extra neck bones. Blood still reaches the brain. "
                                      "Try it and you would faint.")
    monkeypatch.setattr(script_gen, "pick_backend", lambda pref: "claude_code")
    monkeypatch.setattr(script_gen, "_draft", lambda *a, **k: script_gen._Visuals(items=[
        script_gen._Visual(visual_keyword=k_) for k_ in ("owl turning head", "owl skeleton neck", "blood vessels")]))
    script_gen.assign_visuals(script)
    assert [ln["visual_keyword"] for ln in script["lines"]] == ["owl skeleton neck", "blood vessels"]
    assert script["visual_fallback"] == "owl turning head"


# ------------------------------------------------------------------ subjects, hooks, demand, updater
def _fake_jev(monkeypatch, answers):
    import typesafe_sdk

    class _A:
        def __init__(self, v):
            if isinstance(v, dict): self.probabilities = v; self.choice = max(v, key=v.get); self.confidence = 0.8
            elif isinstance(v, tuple): self.score = v[0]
            else: self.noul = v
    class _R:
        pass
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def system_one(self, **k):
            r = _R(); r.answers = {key: _A(v) for key, v in answers(k["questions"]).items()}; return r
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _C)


def test_subject_screening_prefers_curious_fresh_subjects_and_never_a_repeat(monkeypatch):
    from shorts_pipeline import subjects
    monkeypatch.setattr(subjects, "has_typesafe", lambda: True)
    cands = [{"subject": "Noah's ark", "surprise": "x"}, {"subject": "Eutychus falls from a window", "surprise": "y"}, {"subject": "a genealogy list", "surprise": "z"}]

    def answers(questions):
        out = {}
        for k in questions:
            i = int(k.split("_")[1])
            if k.startswith("cur"): out[k] = ((2.8 if i == 1 else 2.9 if i == 0 else 0.3),)
            elif k.startswith("vis"): out[k] = ((1.8, 1.9, 0.5)[i],)
            elif k.startswith("dem"): out[k] = (0.2, 0.6, 0.0)[i]
            else: out[k] = (0.95, 0.05, 0.02)[i]          # Noah's ark is already made
        return out
    _fake_jev(monkeypatch, answers)
    ranked = subjects.screen(cands, {"topic": "bible stories", "format": "storytime"}, ["Noah Builds the Ark"], ["why did eutychus fall"])
    assert ranked[0]["subject"].startswith("Eutychus") and ranked[-1]["subject"] == "Noah's ark"   # the repeat sinks to the bottom


def test_hook_tournament_replaces_only_a_clearly_better_hook_that_still_loops(monkeypatch):
    from shorts_pipeline import hooks
    monkeypatch.setattr(hooks, "has_typesafe", lambda: True)
    monkeypatch.setattr(hooks, "alternatives", lambda s, b, backend="auto": ["A dead man stood up when he touched these bones.", "Here is a story about Elisha."])
    monkeypatch.setattr(hooks, "_max_words", lambda: 13)
    script = {"hook": "Can a dead prophet raise another dead man?", "lines": [{"text": "In Second Kings, Elisha dies."}], "cta": "Which leaves one question:",
              "full_text": "x", "title": "T"}

    def answers(questions):
        out = {}
        for k in questions:
            i = int(k.split("_")[1])
            if k.startswith("stop"): out[k] = ((2.0, 3.0, 1.0)[i],)
            elif k.startswith("spec"): out[k] = (0.7, 0.95, 0.3)[i]
            else: out[k] = (0.9, 0.8, 0.9)[i]
        return out
    _fake_jev(monkeypatch, answers)
    out = hooks.run(dict(script), {}, loop=True)
    assert out["hook"].startswith("A dead man stood up") and out["full_text"].startswith("A dead man stood up")
    # a challenger that breaks the loop is never chosen
    def answers2(questions):
        o = answers(questions)
        for k in o:
            if k.startswith("loop_1"): o[k] = 0.1
        return o
    _fake_jev(monkeypatch, answers2)
    out2 = hooks.run(dict(script), {}, loop=True)
    assert out2["hook"] == script["hook"]


def test_demand_phrases_are_deduplicated_and_ranked_by_source_count(monkeypatch):
    from shorts_pipeline import demand
    monkeypatch.setattr(demand, "_autocomplete", lambda q, ds: ["why do octopuses have three hearts", "octopus toys for sale"] if ds else ["why do octopuses have three hearts?"])
    monkeypatch.setattr(demand, "_reddit", lambda topic, limit=25: ["Why do octopuses change colour?"])
    rows = demand.raw_phrases("octopus")
    assert rows[0]["text"] == "why do octopuses have three hearts" and rows[0]["sources"] == ["google", "youtube"]
    assert any(r["text"] == "why do octopuses change colour" and r["sources"] == ["reddit"] for r in rows)


def test_updater_refuses_to_overwrite_local_changes(monkeypatch):
    from shorts_pipeline import updater
    monkeypatch.setattr(updater, "check", lambda fetch=True: {"local": "aaa", "remote": "bbb", "behind": 2, "ahead": 0,
                                                              "changes": ["x", "y"], "dirty": ["config.yaml"]})
    r = updater.apply(restart=False)
    assert not r["ok"] and "config.yaml" in r["message"]
    monkeypatch.setattr(updater, "check", lambda fetch=True: {"local": "aaa", "remote": "aaa", "behind": 0, "ahead": 0, "changes": [], "dirty": []})
    assert updater.apply(restart=False) == {"ok": True, "updated": False, "message": "Already up to date (aaa)."}
