"""Sequels of winners, Shorts from viewers' requests, and the exploration budget."""
import time

import pytest

import shorts_pipeline.scheduler as sch
from shorts_pipeline import script_gen, specials, upload_queue
from shorts_pipeline.storage import save_json


def _up(i, vph, views=500, pct=None, title=None):
    return {"id": f"v{i}", "title": title or f"Video {i}", "views_per_hour": vph, "views": views, "age_hours": 100,
            "privacy": "public", "format": "single_fact", "topic": "animals_wildlife", "transcript": "x",
            **({"avg_view_pct": pct} if pct is not None else {})}


@pytest.fixture
def special_state(tmp_path, monkeypatch):
    monkeypatch.setattr(specials, "STATE_PATH", tmp_path / "specials.json")
    monkeypatch.setattr(specials, "config", lambda: dict(specials.DEFAULTS))
    return tmp_path


def test_sequel_candidates_are_clear_winners_only(special_state, monkeypatch):
    ups = [_up(i, 0.1, pct=50) for i in range(12)] + [
        _up(100, 5.0, views=900, pct=70, title="Coral Pulls Itself Shut"),          # winner
        _up(101, 5.0, views=120, pct=70),                                          # too few views
        _up(102, 5.0, views=900, pct=30),                                          # weak retention
        _up(103, 5.0, views=900, pct=70, title="Coral Pulls Itself Shut (Part 2)")]  # already a sequel
    monkeypatch.setattr("shorts_pipeline.channel.labelled_uploads", lambda: ups)
    assert [c["id"] for c in specials.sequel_candidates()] == ["v100"]


def test_one_sequel_a_day_then_ideas(special_state, monkeypatch):
    monkeypatch.setattr(specials, "sequel_candidates", lambda: [{"id": "v1", "title": "T", "lift": 9.0}])
    monkeypatch.setattr(specials, "idea_candidates", lambda: [{"id": "c1", "text": "do sharks next!"}])
    assert specials.next_special() == {"sequel_of": "v1"}
    specials.record("sequel", "v1", "out1")
    assert specials.next_special() == {"idea": "c1"}
    specials.record("idea", "c1", "out2")
    assert specials.next_special() is None


def test_ideas_come_from_triaged_comments(special_state, monkeypatch):
    monkeypatch.setattr("shorts_pipeline.comments._state", lambda: {"threads": {
        "c1": {"text": "Can you do one about sharks?", "idea": 0.9, "status": "drafted", "likes": 3},
        "c2": {"text": "nice", "idea": 0.1, "status": "no_reply"},
        "c3": {"text": "make a video on my channel", "idea": 0.8, "status": "spam"}}})
    assert [c["id"] for c in specials.idea_candidates()] == ["c1"]


def test_sequel_may_share_the_subject_of_part_one(monkeypatch):
    seen = {}
    monkeypatch.setattr("shorts_pipeline.history.known_videos", lambda: [{"title": "Coral Pulls Itself Shut", "hook": ""},
                                                                         {"title": "Other Video", "hook": ""}])
    monkeypatch.setattr("shorts_pipeline.history.avoid_titles", lambda bp: [])

    def fake_write(**kw):
        seen["check"] = kw["repeat_check"]
        return {}
    monkeypatch.setattr(script_gen, "_write_with_qa", fake_write)
    calls = []
    monkeypatch.setattr("shorts_pipeline.history.find_repeat", lambda s, known: calls.append([k["title"] for k in known]))
    script_gen.generate_script({"format": "f", "topic": "t", "hook_style": "h"}, allow_repeat_of=["Coral Pulls Itself Shut"])
    seen["check"]({"title": "x"})
    assert calls[0] == ["Other Video"]


def test_exploration_picks_a_never_made_pair(monkeypatch):
    monkeypatch.setattr("shorts_pipeline.specials.config", lambda: dict(specials.DEFAULTS, explore_share=1.0))
    monkeypatch.setattr("shorts_pipeline.channel.labelled_uploads", lambda: [{"format": "listicle_facts", "topic": "space_astronomy"}])
    monkeypatch.setattr("shorts_pipeline.history.known_videos", lambda: [])
    bps = [{"format": "listicle_facts", "topic": "space_astronomy", "opportunity": 5.0},
           {"format": "myth_busting", "topic": "food_drink", "opportunity": 1.0},
           {"format": "comedy_skit", "topic": "pets", "opportunity": 1.0, "stretch": True}]
    assert sch.explore_choice(bps, {"skip_stretch": True}) == 2
    monkeypatch.setattr("shorts_pipeline.specials.config", lambda: dict(specials.DEFAULTS, explore_share=0.0))
    assert sch.explore_choice(bps, {"skip_stretch": True}) is None


def test_experiments_get_a_fair_upload_priority(tmp_path, monkeypatch):
    monkeypatch.setattr("shorts_pipeline.judge.has_typesafe", lambda: False)
    monkeypatch.setattr("shorts_pipeline.channel.blueprint_performance", lambda: {})
    weak_qa = {"qa": {"hook_strength": {"score": 0.3}}}
    plain = upload_queue.score_short(tmp_path, {"blueprint": {"format": "x", "topic": "y"}}, weak_qa)["priority"]
    explore = upload_queue.score_short(tmp_path, {"blueprint": {"format": "x", "topic": "y", "explore": True}}, weak_qa)["priority"]
    assert plain < upload_queue.config()["min_priority"] <= explore
