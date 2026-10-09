"""Second-chance titles, the watchdog's process lookup, and encoder selection."""
import time

import pytest

from shorts_pipeline import render, retitle, watchdog
from shorts_pipeline.storage import load_json, save_json


@pytest.fixture
def channel(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(retitle, "OUTPUT_DIR", out)
    monkeypatch.setattr(retitle, "DATA_DIR", tmp_path)
    monkeypatch.setattr(retitle, "config", lambda: dict(retitle.DEFAULTS))
    now = time.time()
    videos = [{"id": f"v{i}", "views_per_hour": 1.0 + i, "views": 100, "age_hours": 72, "privacy": "public",
               "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 72 * 3600))} for i in range(12)]
    videos[0]["views_per_hour"] = 0.01                        # the weakest
    save_json(tmp_path / "channel_stats.json", {"videos": videos})
    for vid, title in (("v0", "Weak Title"), ("v11", "Strong Title")):
        d = out / vid
        d.mkdir()
        save_json(d / "meta.json", {"youtube_id": vid, "privacy": "public", "title": title})
        save_json(d / "script.json", {"title_search": {"candidates": [
            {"title": title, "tap": .5, "accurate": .9, "natural": .9},
            {"title": "A Better Title", "tap": .3, "accurate": .9, "natural": .9},
            {"title": "A Misleading Title", "tap": .6, "accurate": .2, "natural": .9}]}})
    changed = []
    monkeypatch.setattr(retitle, "_set_title", lambda vid, t: changed.append((vid, t)))
    return out, changed


def test_only_the_weak_short_gets_its_best_accurate_alternative_once(channel):
    out, changed = channel
    r = retitle.run()
    assert changed == [("v0", "A Better Title")] and len(r["retitled"]) == 1
    meta = load_json(out / "v0" / "meta.json")
    assert meta["title"] == "A Better Title" and meta["retitle"]["old"] == "Weak Title"
    retitle.run()
    assert len(changed) == 1                                  # never twice


def test_the_change_is_judged_later(channel, monkeypatch):
    out, _ = channel
    retitle.run()
    meta = load_json(out / "v0" / "meta.json")
    meta["retitle"]["at"] -= 49 * 3600
    save_json(out / "v0" / "meta.json", meta)
    stats = load_json(retitle.DATA_DIR / "channel_stats.json")
    stats["videos"][0]["views"] = 100 + 49 * 2                # ~2 views/h after the change, 0.01 before
    save_json(retitle.DATA_DIR / "channel_stats.json", stats)
    assert retitle.run()["judged"] == 1
    assert load_json(out / "v0" / "meta.json")["retitle"]["verdict"] == "improved"


def test_watchdog_reads_the_listening_process(monkeypatch):
    class _R:
        stdout = ("  TCP    127.0.0.1:8787   0.0.0.0:0   LISTENING   4242\n"
                  "  TCP    127.0.0.1:55060  0.0.0.0:0   LISTENING   99\n")
    monkeypatch.setattr(watchdog.subprocess, "run", lambda *a, **k: _R())
    assert watchdog._listening_pid(8787) == 4242 and watchdog._listening_pid(9999) is None


def test_encoder_setting_overrides_detection(monkeypatch):
    monkeypatch.setattr("shorts_pipeline.config.load_config", lambda: {"production": {"encoder": "libx264"}})
    assert render.probe_encoder() == "libx264"
    monkeypatch.setattr("shorts_pipeline.config.load_config", lambda: {"production": {"encoder": "nonsense"}})
    assert render.probe_encoder() == "libx264"
