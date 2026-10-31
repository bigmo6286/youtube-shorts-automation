"""Fast trend reaction, visual polish (crossfades, push-ins, hook flash) and sound design."""
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from shorts_pipeline import captions, publish_times, render, sfx, trends, upload_queue
from shorts_pipeline.storage import load_json, save_json
from shorts_pipeline.tools import ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>tokyo open</title><ht:approx_traffic>500+</ht:approx_traffic><pubDate>Sat, 31 Oct 2026 02:40:00 -0700</pubDate>
<ht:news_item><ht:news_item_title>Fernandez reaches final</ht:news_item_title><ht:news_item_snippet/><ht:news_item_url>https://e.com/a</ht:news_item_url><ht:news_item_source>ESPN</ht:news_item_source></ht:news_item></item>
<item><title>octopus garden discovery</title><ht:approx_traffic>50K+</ht:approx_traffic><pubDate>Sat, 31 Oct 2026 01:00:00 -0700</pubDate>
<ht:news_item><ht:news_item_title>Scientists find thousands of octopuses nesting</ht:news_item_title><ht:news_item_snippet>Deep off California, a warm spring draws them.</ht:news_item_snippet><ht:news_item_url>https://n.com/b</ht:news_item_url><ht:news_item_source>Nature</ht:news_item_source></ht:news_item></item>
<item><title>plane crash</title><ht:approx_traffic>1M+</ht:approx_traffic><pubDate>Sat, 31 Oct 2026 01:00:00 -0700</pubDate></item>
</channel></rss>"""


def test_trend_feed_parsing():
    assert trends.parse_traffic("500+") == 500 and trends.parse_traffic("20K+") == 20000 and trends.parse_traffic("1M+") == 1000000
    items = trends.parse_feed(FEED)
    assert [i["title"] for i in items] == ["tokyo open", "octopus garden discovery", "plane crash"]
    assert items[1]["traffic"] == 50000 and items[1]["news"][0]["source"] == "Nature" and items[1]["news"][0]["snippet"].startswith("Deep")
    assert items[2]["news"] == []


class _A:
    def __init__(self, **k):
        self.__dict__.update(k)


class _R:
    def __init__(self, answers):
        self.answers = answers


def _client(fits, risks, topics):
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def system_one(self, state, questions):
            ans = {}
            for i, (f, r, t) in enumerate(zip(fits, risks, topics)):
                ans[f"fit_{i}"] = _A(score=f)
                ans[f"risk_{i}"] = _A(noul=r)
                ans[f"topic_{i}"] = _A(choice=t)
            return _R(ans)
    return _C


def test_trend_screening_keeps_safe_explainable_spikes_only(tmp_path, monkeypatch):
    import typesafe_sdk
    monkeypatch.setattr(trends, "STATE_PATH", tmp_path / "trends.json")
    monkeypatch.setattr(trends, "has_typesafe", lambda: True)
    monkeypatch.setattr(trends, "fetch", lambda region=None, timeout=20.0: trends.parse_feed(FEED))
    monkeypatch.setattr(trends, "config", lambda: {**trends.DEFAULTS, "min_traffic": 20000, "min_fit": 0.6})
    # the tennis spike is below min_traffic and never reaches TypeSafe; the crash is risky, the octopus story fits
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _client([2.6, 2.9], [0.05, 0.95], ["animals_wildlife", "other"]))
    best = trends.pick(now=time.time())
    assert best["title"] == "octopus garden discovery" and best["topic"] == "animals_wildlife" and best["fit"] > 0.8
    assert (tmp_path / "trends.json").exists() and "octopus_garden_discovery" in load_json(tmp_path / "trends.json")["candidates"]
    bp, angle, item = trends.blueprint_for("octopus garden discovery")
    assert bp["source"] == "trend" and bp["topic"] == "animals_wildlife" and bp["trend"]["traffic"] == 50000
    assert "Scientists find thousands" in angle and "no speculation" in angle
    # once made, the same spike is not picked again for repeat_days
    trends.record_made("octopus garden discovery", "dir1")
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _client([2.9], [0.95], ["other"]))
    assert trends.pick(now=time.time()) is None


def test_trend_checks_are_paced_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(trends, "STATE_PATH", tmp_path / "trends.json")
    monkeypatch.setattr(trends, "config", lambda: {**trends.DEFAULTS, "checks_per_day": 4, "max_per_day": 1})
    picks = iter([{"title": "octopus garden discovery", "traffic": 50000, "fit": 0.9, "risk": 0.1}, None])
    monkeypatch.setattr(trends, "pick", lambda region=None, now=None: next(picks))
    now = datetime(2026, 11, 2, 9, 0)
    submitted = []
    job = trends.maybe_check(now, lambda kind, params: submitted.append(params) or {"id": "j7"})
    assert job["id"] == "j7" and submitted[0]["trend"] == "octopus garden discovery"
    assert trends.made_today(now.timestamp()) == 1
    # within 6 hours: no new check; after it: capped at one a day
    assert trends.maybe_check(now + timedelta(hours=2), lambda k, p: {"id": "x"}) is None
    assert trends.maybe_check(now + timedelta(hours=7), lambda k, p: {"id": "x"}) is None
    assert len(submitted) == 1


def test_busy_machine_lets_the_trend_check_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(trends, "STATE_PATH", tmp_path / "trends.json")
    monkeypatch.setattr(trends, "config", lambda: dict(trends.DEFAULTS))
    monkeypatch.setattr(trends, "pick", lambda region=None, now=None: {"title": "x", "traffic": 1, "fit": 1, "risk": 0})
    now = datetime(2026, 11, 2, 9, 0)
    assert trends.maybe_check(now, lambda k, p: None) is None
    assert load_json(tmp_path / "trends.json")["last_check"] == 0.0        # looks again on the next tick
    assert trends.maybe_check(now, lambda k, p: {"id": "j1"})["id"] == "j1"


def test_urgent_publish_time_is_within_hours(monkeypatch):
    scores = [0.1] * 24
    scores[20] = 1.0                                                     # the channel's best hour is 20:00 UTC
    monkeypatch.setattr(publish_times, "hour_scores", lambda: {"scores": scores, "videos": [0] * 24, "overall": .1, "n": 0})
    monkeypatch.setattr(publish_times, "_taken", lambda: [])
    base = dict(publish_times.config())
    monkeypatch.setattr(publish_times, "config", lambda: {**base, "review_hours": 2, "horizon_hours": 24,
                                                          "max_per_hour": 2, "publish_gap_minutes": 25})
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    normal = publish_times.choose(now)
    urgent = publish_times.choose(now, urgent=True)
    assert normal.hour == 20                                             # waits for the best hour
    assert timedelta(minutes=30) <= urgent - now <= timedelta(hours=3, minutes=30)   # goes within hours


def test_trend_reaction_jumps_the_queue(tmp_path, monkeypatch):
    base = dict(upload_queue.config())
    monkeypatch.setattr(upload_queue, "config", lambda: {**base, "min_priority": 30})
    d = tmp_path / "t"
    d.mkdir()
    save_json(d / "meta.json", {"title": "x", "blueprint": {"format": "explainer", "topic": "other", "source": "trend"}})
    save_json(d / "script.json", {"hook": "h", "full_text": "t", "qa": {"hook_strength": {"score": 1.0}}})
    pr = upload_queue.score_short(d)
    assert pr["priority"] >= 60 and pr["parts"].get("trend_bonus") == 1.0
    save_json(d / "meta.json", {"title": "x", "blueprint": {"format": "explainer", "topic": "other"}})
    assert upload_queue.score_short(d)["priority"] < 60


def test_cut_times_follow_segment_changes():
    segs = [{"start": 0, "end": 3}, {"start": 3, "end": 3.4}, {"start": 3.4, "end": 7}, {"start": 7, "end": 10}]
    assert sfx.cut_times(segs, offset=1.0, min_gap=0.8) == [4.0, 8.0]     # the 0.4 s change is too close to the one before
    assert sfx.cut_times([{"start": 0, "end": 5}]) == []


@needs_ffmpeg
def test_sfx_track_and_polished_render(tmp_path, monkeypatch):
    monkeypatch.setattr(sfx, "SFX_DIR", tmp_path / "sfx")
    track = sfx.build_track([1.5], 0.0, 3.0, tmp_path / "fx.wav", cfg={**sfx.DEFAULTS})
    assert track.exists() and (tmp_path / "sfx" / "whoosh.wav").exists() and (tmp_path / "sfx" / "hit.wav").exists()
    assert 2.9 <= render.probe_duration(track) <= 3.1 and render.probe_channels(track) == 2
    assert sfx.build_track([], None, 3.0, tmp_path / "none.wav", cfg={**sfx.DEFAULTS}) is None

    def lavfi(out, source, seconds, extra=()):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", source, "-t", str(seconds), *extra, str(out)], check=True)
        return out
    a = lavfi(tmp_path / "a.mp4", "color=c=navy:s=640x360:r=30", 2, ["-pix_fmt", "yuv420p"])
    b = lavfi(tmp_path / "b.mp4", "color=c=darkgreen:s=360x640:r=30", 2, ["-pix_fmt", "yuv420p"])
    voice = lavfi(tmp_path / "voice.mp3", "sine=frequency=220:sample_rate=24000", 3)
    music = lavfi(tmp_path / "music.mp3", "sine=frequency=110:sample_rate=44100", 2)
    words = [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.45} for i, w in enumerate("this is a quick render test".split())]
    ass = captions.write_ass(words, tmp_path / "captions.ass", full_text="This is a quick render test.")
    captions.add_hook_overlay(ass, "QUICK TEST", 1.5)
    assert "\\fscx112" in ass.read_text(encoding="utf-8")                # the hook pops in
    segs = [{"path": str(a), "start": 0, "end": 1.5}, {"path": str(b), "start": 1.5, "end": 3.0}]
    out = render.render(segs, voice, ass, tmp_path / "short.mp4", total_seconds=3.0, music_path=music,
                        polish={"crossfade": 0.25, "push_in": True, "hook_flash": True}, sfx_path=track)
    assert 2.8 <= render.probe_duration(out) <= 3.5                       # the crossfade did not shorten the video
    # the same render without music still mixes the effects under the voice
    out2 = render.render(segs, voice, ass, tmp_path / "short2.mp4", total_seconds=3.0,
                         polish={"crossfade": 0.25, "push_in": False, "hook_flash": False}, sfx_path=track)
    assert render.probe_channels(out2) == 2 and 2.8 <= render.probe_duration(out2) <= 3.5
