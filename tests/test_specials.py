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


# ------------------------------------------------------------------ topic series
def test_topic_series_rotates_suited_formats_and_names_the_source(monkeypatch, tmp_path):
    from shorts_pipeline import topics, script_gen
    monkeypatch.setattr(topics, "STATE_PATH", tmp_path / "topics.json")
    monkeypatch.setattr(topics, "has_typesafe", lambda: True)
    monkeypatch.setattr(topics, "_judge_topic", lambda t: {"formats": {"storytime": 0.6, "history_story": 0.3},
                                                           "label": "religion_spirituality", "attribute_source": True})
    monkeypatch.setattr("shorts_pipeline.channel.blueprint_performance", lambda: {"formats": {}})
    bp = topics.plan("bible stories")
    assert bp["format"] in ("storytime", "history_story") and bp["topic"] == "religion_spirituality"
    assert bp["key"] == "series:bible_stories" and bp["attribute_source"]
    topics.record("bible stories", bp["format"], "dir1")
    nxt = topics.plan("bible stories")
    assert nxt["format"] != bp["format"]                                 # never the same format twice in a row
    assert nxt["hook_style"] != bp["hook_style"]
    prompt = script_gen._prompt(bp, 30, None, avoid=["Noah Builds the Ark"])
    assert "SERIES on: bible stories" in prompt and "In Genesis" in prompt and "Noah Builds the Ark" in prompt
    assert topics.describe("bible stories")["judged"]                     # judged once, then remembered
    assert topics.next_topic(["bible stories", "space facts"]) == "space facts"


def test_series_avoid_list_covers_every_format_in_the_series(monkeypatch):
    from shorts_pipeline import history
    known = [{"title": "Noah Builds the Ark", "key": "series:bible_stories"},
             {"title": "David and Goliath", "key": "series:bible_stories"},
             {"title": "Why Owls Turn Their Heads", "key": "explainer|animals_wildlife"}]
    monkeypatch.setattr(history, "known_videos", lambda: known)
    avoid = history.avoid_titles({"format": "listicle_facts", "topic": "religion_spirituality", "key": "series:bible_stories"}, limit=2)
    assert avoid == ["Noah Builds the Ark", "David and Goliath"]


# ------------------------------------------------------------------ post-mortems, calendar, usage, music mood, approval, render check
def test_postmortem_lessons_count_repeatable_causes(monkeypatch, tmp_path):
    from shorts_pipeline import postmortem
    from shorts_pipeline.storage import save_json
    monkeypatch.setattr(postmortem, "STATE_PATH", tmp_path / "pm.json")
    reviews = {}
    for i in range(4):
        reviews[f"l{i}"] = {"outcome": "loser", "cause": "subject" if i < 3 else "hook", "repeatable": 0.8, "at": i, "title": "x", "factor": 0.4}
    for i in range(3):
        reviews[f"w{i}"] = {"outcome": "winner", "cause": "hook", "repeatable": 0.9, "at": 10 + i, "title": "y", "factor": 2.0}
    reviews["n"] = {"outcome": "loser", "cause": "noise", "repeatable": 0.1, "at": 20, "title": "z", "factor": 0.5}
    save_json(postmortem.STATE_PATH, {"reviews": reviews})
    text = postmortem.lessons()
    assert any("3 of the last 4 Shorts that underperformed" in t and "subject" in t for t in text)
    assert any("3 of the last 3 Shorts that won" in t and "hook" in t for t in text)
    assert postmortem.summary()["losers"] == 5


def test_calendar_knows_movable_feasts_and_upcoming_events():
    from datetime import date
    from shorts_pipeline import seasonal
    assert seasonal.easter(2026) == date(2026, 4, 5) and seasonal.easter(2024) == date(2024, 3, 31)
    ups = seasonal.upcoming(date(2026, 10, 28), lead_days=6)
    assert [u["name"] for u in ups] == ["Halloween"] and ups[0]["days"] == 3
    tg = seasonal.events_for_year(2026, ["US"])
    assert tg[date(2026, 11, 26)] == "Thanksgiving (US)" and tg[date(2026, 11, 27)] == "Black Friday"


def test_usage_records_per_day_and_per_job(monkeypatch, tmp_path):
    from shorts_pipeline import usage
    monkeypatch.setattr(usage, "PATH", tmp_path / "usage.json")
    monkeypatch.setattr(usage, "_maybe_alert", lambda day: None)
    usage.CURRENT_JOB = "job1"
    usage.record(typesafe_calls=1, typesafe_in=1200, typesafe_out=40)
    usage.record(youtube_units=1600, images=1, pollen=0.005)
    usage.CURRENT_JOB = None
    usage.record(youtube_units=100)
    t = usage.today()
    assert t["typesafe_in"] == 1200 and t["youtube_units"] == 1700 and t["pollen"] == 0.005
    assert usage.job("job1")["youtube_units"] == 1600 and "job1" not in usage.today()
    assert "YouTube API 1,700/10,000" in usage.summary_text()


def test_music_by_mood_skips_the_previous_track(monkeypatch, tmp_path):
    from shorts_pipeline import music
    from shorts_pipeline.storage import save_json
    tracks = [{"file": "epic.mp3", "title": "Epic cinematic", "license": "cc0", "path": "x"},
              {"file": "lofi.mp3", "title": "Lofi chill", "license": "cc0", "path": "y"}]
    monkeypatch.setattr(music, "list_tracks", lambda: tracks)
    monkeypatch.setattr("shorts_pipeline.config.has_typesafe", lambda: True)
    monkeypatch.setattr("shorts_pipeline.config.DATA_DIR", tmp_path)
    save_json(tmp_path / "music_last.json", {"file": "epic.mp3"})

    class _A:
        probabilities = {"epic.mp3": 0.7, "lofi.mp3": 0.3}
    class _R:
        answers = {"track": _A()}
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def system_one(self, **k): return _R()
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _C)
    assert music.pick_for_script({"title": "Battle", "full_text": "x"}, "random")["file"] == "lofi.mp3"
    assert music.pick_for_script({"title": "Battle", "full_text": "x"}, "epic")["file"] == "epic.mp3"   # explicit choice wins


def test_approval_gate_holds_queued_shorts_until_approved(monkeypatch, tmp_path):
    import time
    from shorts_pipeline import upload_queue as q
    from shorts_pipeline.storage import load_json, save_json
    monkeypatch.setattr(q, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(q, "STATE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(q, "config", lambda: dict(q.DEFAULTS, auto=True, min_priority=30, min_gap_minutes=0))
    monkeypatch.setattr(q, "limit", lambda state=None: 10)
    monkeypatch.setattr(q, "approval_config", lambda: {"enabled": True, "timeout_hours": 12, "auto_approve_on_timeout": True})
    (tmp_path / "token").write_text("t")
    monkeypatch.setattr("shorts_pipeline.upload.TOKEN_PATH", tmp_path / "token")
    for name, asked in (("2026-10-30T10-00-00Z_a", time.time()), ("2026-10-30T09-00-00Z_b", time.time() - 13 * 3600)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "short.mp4").write_bytes(b"x")
        save_json(tmp_path / name / "meta.json", {"title": name, "upload_state": "queued", "upload_priority": 70,
                                                  "approval": "pending", "approval_asked_at": asked})
    nxt, reason = q.next_upload()
    assert nxt and nxt["dir"].endswith("_b")                       # the timed-out one is auto-approved, the fresh one waits
    assert load_json(tmp_path / "2026-10-30T09-00-00Z_b" / "meta.json")["approval"] == "approved"
    q.set_approval(tmp_path / "2026-10-30T10-00-00Z_a", "rejected")
    assert all(not i["dir"].endswith("_a") for i in q.queued())    # rejected: out of the queue, file kept
    from shorts_pipeline import telegram_bot
    monkeypatch.setattr(telegram_bot, "OUTPUT_DIR", tmp_path)
    assert "Approved" in telegram_bot._decide_from_button("approve:2026-10-30T09-00-00Z_b")
    assert "Unknown" in telegram_bot._decide_from_button("approve:../etc")


def test_render_check_severity_policy(monkeypatch, tmp_path):
    from shorts_pipeline import render_check
    monkeypatch.setattr(render_check, "black_seconds", lambda v: 0.0)
    monkeypatch.setattr(render_check, "has_typesafe", lambda: True)
    monkeypatch.setattr(render_check, "_frame", lambda v, at, d: b"frame")
    monkeypatch.setattr(render_check, "_judge_frames", lambda frames, title: [
        {"at": 0.15, "covers_subject": 0.9, "text_cut": 0.1, "blank": 0.05},
        {"at": 0.5, "covers_subject": 0.8, "text_cut": 0.05, "blank": 0.02},
        {"at": 0.85, "covers_subject": 0.1, "text_cut": 0.05, "blank": 0.9}])
    r = render_check.check(tmp_path / "short.mp4", 30.0, "t")
    assert r["severity"] == "severe" and "blank frame at 85%" in r["issues"] and "captions cover the subject" in r["issues"][0]
    monkeypatch.setattr(render_check, "_judge_frames", lambda frames, title: [{"at": 0.5, "covers_subject": 0.1, "text_cut": 0.0, "blank": 0.0}])
    assert render_check.check(tmp_path / "short.mp4", 30.0, "t")["severity"] == "ok"


def test_on_this_day_prefers_old_round_anniversaries_and_skips_tragedies():
    from shorts_pipeline import seasonal
    rows = [{"year": 2023, "text": "A bridge collapses, killing 20"}, {"year": 1926, "text": "Houdini's last performance"},
            {"year": 1976, "text": "A famous film premieres"}, {"year": 2010, "text": "A phone is launched"},
            {"year": 1517, "text": "Luther posts his 95 theses"}]
    picks = seasonal._pick_anniversaries(rows, 2026, 3)
    assert picks[0].startswith("1926 (100 years ago)") and picks[1].startswith("1976 (50 years ago)")
    assert all("collapses" not in p for p in picks) and len(picks) == 3
