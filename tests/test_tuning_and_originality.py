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


# ------------------------------------------------------------------ fact-check policy (TypeSafe verifies the checker)
def test_fact_check_verdicts_follow_the_evidence(monkeypatch):
    from shorts_pipeline import factcheck
    claims = [{"claim": "The bridge opened in 1937", "verdict": "supported", "evidence": "It opened to vehicles on May 28, 1937.", "source_url": "https://a", "correction": "", "core": True},
              {"claim": "Its span is 5,000 feet", "verdict": "supported", "evidence": "The main span is 4,200 feet.", "source_url": "https://b", "correction": "", "core": True},
              {"claim": "Painters use 3 tons of paint a year", "verdict": "supported", "evidence": "", "source_url": "", "correction": "", "core": False}]
    monkeypatch.setattr(factcheck, "has_typesafe", lambda: True)

    class _A:
        def __init__(self, v): self.noul = v
    class _R:
        answers = {"sup_0": _A(0.95), "con_0": _A(0.02), "sup_1": _A(0.05), "con_1": _A(0.9)}
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def system_one(self, **k): return _R()
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _C)
    factcheck._verify_with_typesafe(claims)
    assert [c["verdict"] for c in claims] == ["supported", "contradicted", "unverifiable"]
    result = {"claims": claims, "supported": 1, "contradicted": [claims[1]], "unverifiable": [claims[2]], "sources": ["https://a", "https://b"]}
    assert factcheck.summary(result) == "1/3 claims verified, 1 wrong, 1 unverifiable"
    assert factcheck.needs_review(result)                          # a wrong claim that was not corrected waits for the owner
    result["fixed"] = True
    assert factcheck.summary(result) == "1/3 claims verified, 1 corrected, 1 unverifiable"
    assert not factcheck.needs_review(result)                      # the unverified claim is not a core one
    assert factcheck.sources_block(result).startswith("Sources:\nhttps://a")


# ------------------------------------------------------------------ tighter loop
def test_loop_trim_cuts_the_lead_in_and_ends_right_after_the_last_word(monkeypatch, tmp_path):
    from shorts_pipeline import tts
    ran = []
    monkeypatch.setattr("shorts_pipeline.tools.run", lambda cmd, **k: ran.append(cmd) or (tmp_path / "v.trim.mp3").write_bytes(b"x"))
    (tmp_path / "v.mp3").write_bytes(b"orig")
    words = [{"text": "Hi", "start": 0.62, "end": 0.9}, {"text": "there", "start": 0.95, "end": 1.4}]
    out, body = tts.tighten_loop(tmp_path / "v.mp3", words)
    assert ran and "-ss" in ran[0] and ran[0][ran[0].index("-ss") + 1] == "0.570"
    assert out[0]["start"] == 0.05 and out[-1]["end"] == 0.83 and body == 0.83 + tts.LOOP_TAIL
    ran.clear()
    out2, _ = tts.tighten_loop(tmp_path / "v.mp3", [{"text": "Hi", "start": 0.06, "end": 0.5}])
    assert not ran and out2[0]["start"] == 0.06                    # already tight: nothing re-encoded


# ------------------------------------------------------------------ drop-off learning
def test_retention_measures_sections_and_writes_guidance(monkeypatch, tmp_path):
    from shorts_pipeline import retention
    from shorts_pipeline.storage import save_json
    monkeypatch.setattr(retention, "CURVES_PATH", tmp_path / "curves.json")
    monkeypatch.setattr("shorts_pipeline.tuning.TUNING_PATH", tmp_path / "tuning.json")
    curve_good = [1.2 - 0.004 * i for i in range(100)]              # replays, gentle slope
    curve_bad = [1.0 - 0.012 * i if i < 20 else 0.76 - 0.003 * (i - 20) for i in range(100)]   # loses a quarter in the hook
    outputs = {}
    store = {}
    for i in range(8):
        vid = f"v{i}"
        good = i % 2 == 0
        script = {"hook": "Short hook here." if good else "This is a much longer and slower opening sentence for the hook.",
                  "lines": [{"text": "Line one of the body."}, {"text": "Line two of the body."}], "cta": "And then" if good else "And that is why you should always remember this"}
        meta = {"duration": 30.0, "blueprint": {"hook_style": "bold_claim" if good else "question"}}
        outputs[vid] = (meta, script)
        store[vid] = {"curve": curve_good if good else curve_bad, "views": 500, "at": 1}
    save_json(retention.CURVES_PATH, store)
    monkeypatch.setattr(retention, "_outputs_by_video", lambda: outputs)
    m = retention.measure(curve_bad, *outputs["v1"])
    assert m["worst_section"] == "hook" and m["hook_hold"] < m["start"]
    r = retention.learn()
    assert r["videos"] == 8 and r["hook_hold"] is not None
    text = " ".join(r["guidance"])
    assert "words or fewer" in text and "bold claim" in text and "closing bridge" in text
    assert retention.guidance() == r["guidance"]
