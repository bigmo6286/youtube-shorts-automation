"""Pattern interrupt at the drop-off point, traffic sources from Analytics, audio mastering."""
import shutil
import subprocess
from datetime import date

import pytest

from shorts_pipeline import captions, interrupt, render, sfx, traffic
from shorts_pipeline.storage import load_json, save_json
from shorts_pipeline.tools import ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


# ---------------------------------------------------------------------------------------------------- interrupt

def test_retention_learns_where_viewers_leave(monkeypatch, tmp_path):
    from shorts_pipeline import retention
    monkeypatch.setattr(retention, "CURVES_PATH", tmp_path / "curves.json")
    monkeypatch.setattr("shorts_pipeline.tuning.TUNING_PATH", tmp_path / "tuning.json")
    # gentle slope, then a cliff between 38% and 44%, then gentle again
    curve = [1.0 - 0.002 * i if i < 38 else (0.92 - 0.05 * (i - 38) if i < 44 else 0.62 - 0.002 * (i - 44)) for i in range(100)]
    script = {"hook": "Short hook here.", "lines": [{"text": f"Body line number {i} of the script here."} for i in range(6)], "cta": "And then"}
    meta = {"duration": 30.0, "blueprint": {"hook_style": "question"}}
    outputs = {f"v{i}": (meta, script) for i in range(7)}
    save_json(retention.CURVES_PATH, {k: {"curve": curve, "views": 400, "at": 1} for k in outputs})
    monkeypatch.setattr(retention, "_outputs_by_video", lambda: outputs)
    m = retention.measure(curve, meta, script)
    assert 0.36 <= m["drop_at"] <= 0.46
    r = retention.learn()
    assert r["drop_at_videos"] == 7 and 0.36 <= r["drop_at"] <= 0.46
    assert retention.drop_point() == r["drop_at"]
    assert interrupt.drop_at() == (r["drop_at"], "learned")


def _script():
    return {"hook": "Three word hook.", "lines": [{"text": f"Line {i} has four words.", "visual_keyword": "x"} for i in range(5)],
            "cta": "And then", "full_text": ""}


def test_interrupt_picks_the_line_before_the_drop_and_times_it(monkeypatch):
    monkeypatch.setattr(interrupt, "config", lambda: dict(interrupt.DEFAULTS))
    s = _script()
    assert interrupt.line_index(s, 0.42) == 1                   # 30 words, the drop near word 12.6: line 2 (index 1) starts at word 8
    assert interrupt.line_index(s, 0.05) == 1 and interrupt.line_index(s, 0.99) == 3      # never the first or the last line
    assert interrupt.line_index({"hook": "a", "lines": [{"text": "b"}, {"text": "c"}], "cta": "d"}, 0.4) is None
    s["interrupt"] = {"line": 1}
    words = [{"text": "w", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(30)]
    assert interrupt.timing(s, words) == 4.0                     # word 8 starts at 4.0 s
    note = interrupt.prompt_note(40)
    assert "RE-HOOK" in note and "around spoken word" in note


class _A:
    def __init__(self, **k):
        self.__dict__.update(k)


def _ts_client(answers):
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def system_one(self, state, questions):
            return _A(answers={k: v for k, v in answers.items() if k in questions})
    return _C


def test_interrupt_rewrites_a_weak_line_only_when_typesafe_agrees(monkeypatch):
    import typesafe_sdk
    monkeypatch.setattr(interrupt, "config", lambda: dict(interrupt.DEFAULTS))
    monkeypatch.setattr(interrupt, "has_typesafe", lambda: True)
    monkeypatch.setattr(interrupt, "drop_at", lambda: (0.42, "default"))
    monkeypatch.setattr(interrupt, "_ask_claude", lambda script, idx, backend: interrupt._Rehook(
        rehook_line="But the next part is what nobody expected.", visual_keyword="dark ocean floor", card_text="52 HERTZ"))
    weak = {"rehook_o": _A(score=0.9), "rehook_r": _A(score=2.7), "faithful": _A(noul=0.9), "flows": _A(noul=0.9), "card_ok": _A(noul=0.9)}
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _ts_client(weak))
    s = interrupt.plan(_script())
    assert s["interrupt"]["rewritten"] and s["lines"][1]["text"].startswith("But the next part")
    assert s["lines"][1]["visual_keyword"] == "dark ocean floor" and s["interrupt"]["card_text"] == "52 HERTZ"
    assert "nobody expected" in s["full_text"] and s["word_count"] == len(s["full_text"].split())
    # a strong original stays; a card that is not in the script is dropped
    strong = {**weak, "rehook_o": _A(score=2.6), "card_ok": _A(noul=0.2)}
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _ts_client(strong))
    s2 = interrupt.plan(_script())
    assert not s2["interrupt"]["rewritten"] and s2["lines"][1]["text"] == "Line 1 has four words." and s2["interrupt"]["card_text"] is None
    # a rewrite that adds a claim is refused even when it reads stronger
    unfaithful = {**weak, "faithful": _A(noul=0.3)}
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _ts_client(unfaithful))
    assert not interrupt.plan(_script())["interrupt"]["rewritten"]


def test_callout_card_is_added_to_the_captions(tmp_path):
    words = [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.45} for i, w in enumerate("this is a quick render test".split())]
    ass = captions.write_ass(words, tmp_path / "c.ass", full_text="This is a quick render test.")
    captions.add_card_event(ass, "1 billion tons", 1.5, 1.3)
    text = ass.read_text(encoding="utf-8")
    assert "Style: Callout," in text and "0:00:01.50,0:00:02.80,Callout" in text and "1 BILLION TONS" in text and "\\fscx135" in text


@needs_ffmpeg
def test_extra_hits_and_punch_and_mastering(tmp_path, monkeypatch):
    monkeypatch.setattr(sfx, "SFX_DIR", tmp_path / "sfx")
    track = sfx.build_track([], None, 3.0, tmp_path / "fx.wav", cfg={**sfx.DEFAULTS}, hits=[1.5])
    assert track is not None and 2.9 <= render.probe_duration(track) <= 3.1          # a hit alone is enough for a track

    def lavfi(out, source, seconds, extra=()):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", source, "-t", str(seconds), *extra, str(out)], check=True)
        return out
    a = lavfi(tmp_path / "a.mp4", "color=c=navy:s=640x360:r=30", 2, ["-pix_fmt", "yuv420p"])
    b = lavfi(tmp_path / "b.mp4", "color=c=darkgreen:s=360x640:r=30", 2, ["-pix_fmt", "yuv420p"])
    voice = lavfi(tmp_path / "voice.mp3", "sine=frequency=220:sample_rate=24000", 3, ["-af", "volume=0.05"])   # a quiet voice
    music = lavfi(tmp_path / "music.mp3", "sine=frequency=110:sample_rate=44100", 2)
    words = [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.45} for i, w in enumerate("this is a quick render test".split())]
    ass = captions.write_ass(words, tmp_path / "captions.ass", full_text="This is a quick render test.")
    captions.add_card_event(ass, "QUICK TEST", 1.5, 1.0)
    segs = [{"path": str(a), "start": 0, "end": 1.5}, {"path": str(b), "start": 1.5, "end": 3.0}]
    report = {}
    out = render.render(segs, voice, ass, tmp_path / "short.mp4", total_seconds=3.0, music_path=music,
                        polish={"crossfade": 0.25, "push_in": True, "hook_flash": True, "punch_at": 1.5}, sfx_path=track,
                        mastering=dict(render.MASTER_DEFAULTS), report=report)
    assert 2.8 <= render.probe_duration(out) <= 3.5 and not (tmp_path / "mix.wav").exists()
    assert report["input_lufs"] is not None and report["input_lufs"] < -20          # the quiet mix was measured...
    got = render.measure_loudness(out, render.MASTER_DEFAULTS)
    assert got and -16.0 <= got["input_i"] <= -12.0                                  # ...and lifted to the target
    # without mastering the quiet mix stays quiet
    out2 = render.render(segs, voice, ass, tmp_path / "short2.mp4", total_seconds=3.0, polish=None, sfx_path=None)
    quiet = render.measure_loudness(out2, render.MASTER_DEFAULTS)
    assert quiet and quiet["input_i"] < -20


# ---------------------------------------------------------------------------------------------------- traffic

class _Reports:
    def __init__(self, rows_for):
        self.rows_for = rows_for

    def query(self, **kw):
        rows = self.rows_for(kw)
        return _A(execute=lambda: {"rows": rows})


def _rows(kw):
    dims, filt = kw.get("dimensions"), kw.get("filters") or ""
    if dims == "insightTrafficSourceType" and filt.startswith("video=="):
        return [["YT_SEARCH", 30], ["SHORTS", 10]]
    if dims == "insightTrafficSourceType":
        span = (date.fromisoformat(kw["endDate"]) - date.fromisoformat(kw["startDate"])).days + 1
        if span == 3:                                   # the last three days: the feed went quiet
            return [["SHORTS", 60], ["YT_SEARCH", 30]]
        if span == 14:                                  # the two weeks before
            return [["SHORTS", 1400], ["YT_SEARCH", 200]]
        return [["SHORTS", 700], ["YT_SEARCH", 200], ["RELATED_VIDEO", 100]]
    if dims == "insightTrafficSourceDetail" and "YT_SEARCH" in filt:
        return [["octopus garden", 120], ["neutron star weight", 80]]
    if dims == "insightTrafficSourceDetail" and "RELATED_VIDEO" in filt:
        return [["abc123", 60]]
    return []


@pytest.fixture
def traffic_env(monkeypatch, tmp_path):
    monkeypatch.setattr(traffic, "TRAFFIC_PATH", tmp_path / "traffic.json")
    monkeypatch.setattr(traffic, "config", lambda: dict(traffic.DEFAULTS))
    monkeypatch.setattr("shorts_pipeline.analytics._service", lambda interactive=False: _A(reports=lambda: _Reports(_rows)))
    monkeypatch.setattr(traffic, "_titles_for", lambda ids: {"abc123": {"title": "Octopus facts", "channel": "SeaTV"}})
    monkeypatch.setattr(traffic, "_recent_uploads", lambda limit: [{"id": "v1", "age_hours": 24.0}])
    return tmp_path


def test_traffic_fetch_reads_search_terms_suggested_videos_and_splits(traffic_env):
    data = traffic.fetch()
    assert [t["term"] for t in data["search_terms"]] == ["octopus garden", "neutron star weight"]
    assert data["suggested_from"][0]["title"] == "Octopus facts" and data["sources"]["SHORTS"] == 700
    assert data["per_video"]["v1"]["sources"] == {"YT_SEARCH": 30, "SHORTS": 10}
    assert traffic.terms_for("octopus facts") == ["octopus garden"] and traffic.terms_for("bible stories") == []
    s = traffic.split_for("v1")
    assert s["search"] == 0.75 and s["channel_search"] == 0.2 and s["feed"] == 0.25
    drop = traffic.feed_check(data)
    assert drop and drop["recent"] == 20 and drop["before"] == 100
    assert traffic.summary_text().startswith("Traffic (28 d): Shorts feed 70%")
    assert "⚠️" in traffic.report_text() and "octopus garden" in traffic.report_text()
    ov = traffic.overview()
    assert ov["shares"][0] == {"source": "Shorts feed", "share": 0.7} and ov["feed_drop"]


def test_traffic_sync_alerts_once_on_a_feed_drop(traffic_env, monkeypatch):
    monkeypatch.setattr("shorts_pipeline.analytics.connected", lambda: True)
    sent = []
    monkeypatch.setattr("shorts_pipeline.alerts.send", lambda key, text, repeat_hours=3: sent.append((key, repeat_hours)) or True)
    line = traffic.sync()
    assert "FEED DROP" in line and sent == [("feed_drop", 24)]


def test_proven_search_terms_lead_the_title_phrases(monkeypatch):
    from shorts_pipeline import titles
    monkeypatch.setattr(titles, "suggestions", lambda q, cfg=None: ["octopus garden discovery facts", "cheap octopus toys"])
    monkeypatch.setattr("shorts_pipeline.traffic.terms_for", lambda topic, limit=8: ["octopus garden"])
    script = {"title": "The Octopus Garden Nobody Expected", "thumbnail_text": "octopus garden", "hook": "", "full_text": ""}
    phrases = titles.search_phrases(script)
    assert phrases[0] == "octopus garden" and "octopus garden discovery facts" in phrases


def test_demand_note_mentions_the_channels_own_searches(monkeypatch):
    from shorts_pipeline import demand
    monkeypatch.setattr(demand, "questions", lambda topic, limit=8: [])
    monkeypatch.setattr("shorts_pipeline.traffic.terms_for", lambda topic, limit=8: ["neutron star weight"])
    note = demand.prompt_note("space facts")
    assert "THIS channel" in note and "neutron star weight" in note
