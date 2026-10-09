"""Learned daily volume and length, and the originality guard."""
from types import SimpleNamespace

from shorts_pipeline import judge, originality, tuning


def _video(i, day, n_vph, duration=40, pct=None):
    return {"id": f"v{i}", "published": f"2026-10-{day:02d}T12:00:00Z", "views_per_hour": n_vph, "duration": duration,
            "age_hours": 100, "privacy": "public", **({"avg_view_pct": pct} if pct is not None else {})}


def test_volume_moves_halfway_toward_the_best_bucket():
    vids, i = [], 0
    for day, count, vph in ((1, 4, 2.0), (2, 5, 2.0), (3, 20, 0.1), (4, 22, 0.1)):
        for _ in range(count):
            vids.append(_video(i, day, vph))
            i += 1
    first = tuning.learn_volume(vids, None, ceiling=20, floor=3)
    assert first["target"] in (4, 5) and first["best_bucket"] == "1-5"
    assert first["recommended"] == int(round(20 + (first["target"] - 20) * 0.5))      # halfway from the configured 20
    assert tuning.learn_volume(vids, 3, ceiling=20, floor=3)["recommended"] >= 3      # never below the floor


def test_volume_needs_enough_days():
    assert tuning.learn_volume([_video(0, 1, 1.0)], 7, ceiling=20, floor=3)["recommended"] == 7


def test_shorter_wins_when_it_holds_viewers_and_gets_views(monkeypatch):
    monkeypatch.setattr("shorts_pipeline.analytics.retention_for", lambda vid: None)
    monkeypatch.setattr("shorts_pipeline.channel.labelled_uploads", lambda: [])
    vids = [_video(i, 1, 1.0, duration=20, pct=75) for i in range(6)] + \
           [_video(10 + i, 1, 0.1, duration=40, pct=50) for i in range(10)]
    out = tuning.learn_lengths(vids, default=40, lo=18, hi=55)
    assert out["channel"] == 20


def test_overused_formulas_and_sameness(monkeypatch):
    monkeypatch.setattr(originality, "config", lambda: dict(originality.DEFAULTS))
    recent = [{"title": f"Honey Never Spoils, and {n} More Food Facts", "opening": ""} for n in range(5)] + \
             [{"title": t, "opening": ""} for t in ("The Slowest Marathon Took 54 Years", "Octopuses Have Three Hearts",
                                                   "A Tree Older Than Rome", "Japan Rebuilds One Shrine Every 20 Years",
                                                   "Venus Spins Backwards")]
    over = dict(originality.overused_formulas(recent))
    assert "'..., and N More ...' list title" in over and over["'..., and N More ...' list title"] == 0.5
    assert originality.sameness(recent) == 0.5


def test_voice_rotation_is_stable_per_title(monkeypatch):
    monkeypatch.setattr(originality, "config", lambda: dict(originality.DEFAULTS))
    a = originality.voice_for("Octopuses Have Three Hearts", "en-US-AndrewNeural")
    assert a == originality.voice_for("Octopuses Have Three Hearts", "en-US-AndrewNeural")
    assert a in originality.DEFAULTS["voices"]
    assert len({originality.voice_for(f"Title {i}", "x") for i in range(30)}) > 1
    monkeypatch.setattr(originality, "config", lambda: dict(originality.DEFAULTS, voices=[]))
    assert originality.voice_for("Anything", "en-US-AndrewNeural") == "en-US-AndrewNeural"


def test_template_check_is_asked_with_recent_uploads(monkeypatch):
    seen = {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def system_one(self, state, questions):
            seen["q"], seen["state"] = set(questions), state
            return SimpleNamespace(answers={}, model="x")
    monkeypatch.setattr(judge, "has_typesafe", lambda: True)
    monkeypatch.setattr(judge, "_client", lambda: _Client())
    recent = [{"title": "Honey Never Spoils, and 5 More Food Facts", "opening": "Honey never spoils."}]
    judge.judge_script({"title": "t", "hook": "h", "lines": [], "cta": "c", "full_text": "h c"}, {"format": "f"}, recent=recent)
    assert "template_repeat" in seen["q"] and seen["state"]["recent_uploads"] == recent
