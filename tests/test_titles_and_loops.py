"""Search-driven titles (seeds, suggestions, TypeSafe pick rules) and loop-friendly Shorts (prompt, QA, hook overlay)."""
from types import SimpleNamespace

import pytest

from shorts_pipeline import captions, judge, script_gen, titles

SCRIPT = {"title": "Every Time an Octopus Swims, One of Its Hearts Stops", "hook": "An octopus has three hearts.",
          "visual_fallback": "octopus swimming underwater", "thumbnail_text": "ONE HEART STOPS",
          "full_text": "An octopus has three hearts. One stops whenever it swims."}


def test_seed_queries_are_two_word_subject_phrases():
    seeds = titles.seed_queries(SCRIPT)
    assert seeds[0] == "octopus swimming"
    assert all(len(s.split()) == 2 for s in seeds) and len(seeds) <= 4
    assert any(s.startswith("octopus heart") for s in seeds)


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
