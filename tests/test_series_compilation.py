"""Series navigation, weekly compilations, and the fact-check on your own scripts."""
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from shorts_pipeline import compilation, factcheck, housekeeping, series
from shorts_pipeline.storage import load_json, save_json


def _short(root: Path, name: str, *, series_name: str | None = None, topic: str = "history", **meta):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    bp = {"format": "storytime", "topic": topic}
    if series_name:
        bp.update(series=series_name, key=f"series:{series_name.replace(' ', '_')}")
    base = {"title": f"Title {name[-1]}", "description": "A description.", "hashtags": ["facts"], "blueprint": bp,
            "video": str(d / "short.mp4"), "duration": 40.0}
    base.update(meta)
    save_json(d / "meta.json", base)
    return d


# ---------------------------------------------------------------------------------------------------- series
def test_series_parts_numbered_in_upload_order_and_links_prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(series, "OUTPUT_DIR", tmp_path)
    now = time.time()
    _short(tmp_path, "2026-10-20T10-00-00Z_a", series_name="bible stories", youtube_id="A", privacy="public", uploaded_at=now - 3 * 86400)
    _short(tmp_path, "2026-10-22T10-00-00Z_b", series_name="bible stories", youtube_id="B", privacy="public", uploaded_at=now - 86400)
    _short(tmp_path, "2026-10-23T10-00-00Z_c", series_name="bible stories")                 # not uploaded: no part yet
    _short(tmp_path, "2026-10-23T11-00-00Z_d", series_name="space facts", youtube_id="D", privacy="public", uploaded_at=now)
    new = _short(tmp_path, "2026-10-24T10-00-00Z_e", series_name="bible stories")
    parts = series.parts("bible stories", exclude=new.name)
    assert [p["part"] for p in parts] == [1, 2] and [p["youtube_id"] for p in parts] == ["A", "B"]
    assert load_json(tmp_path / "2026-10-20T10-00-00Z_a" / "meta.json")["series_part"] == 1    # remembered
    info = series.prepare(new, load_json(new / "meta.json"))
    assert info["part"] == 3 and info["previous"]["youtube_id"] == "B" and info["first"]["youtube_id"] == "A"
    assert "Part 3 of the bible stories series." in info["block"]
    assert "youtube.com/shorts/B" in info["block"] and "youtube.com/shorts/A" in info["block"]
    assert series.prepare(new, {"blueprint": {"format": "storytime", "topic": "history"}}) is None   # not a series


def test_series_links_queue_and_flush(tmp_path, monkeypatch):
    monkeypatch.setattr(series, "OUTPUT_DIR", tmp_path)
    now = time.time()
    _short(tmp_path, "2026-10-20T10-00-00Z_a", series_name="bible stories", youtube_id="A", privacy="public", uploaded_at=now - 86400)
    new = _short(tmp_path, "2026-10-24T10-00-00Z_b", series_name="bible stories", youtube_id="B", privacy="private", uploaded_at=now)
    info = series.prepare(new, load_json(new / "meta.json"))
    appended, comments = [], []
    monkeypatch.setattr(series, "_append_description", lambda vid, text, local: appended.append((vid, text)) or True)

    class FakeYT:
        pass
    monkeypatch.setattr(series, "_comments_service", lambda: FakeYT())
    monkeypatch.setattr(series, "_post_comment", lambda yt, vid, text: comments.append((vid, text)) or "c1")
    done = series.link_after_upload(new, load_json(new / "meta.json"), info)
    meta = load_json(new / "meta.json")
    assert meta["series_part"] == 2 and meta["series_name"] == "bible stories"
    # the previous part's description and its "part 2 is out" comment went at once (it is public)...
    assert appended == [("A", "Next part: https://youtube.com/shorts/B")]
    assert comments == [("A", "Part 2 is out: https://youtube.com/shorts/B")]
    # ...the new part's own comment waits until it is public
    assert [a["type"] for a in meta["series_pending"]] == ["comment"] and meta["series_pending"][0]["video"] == "B"
    assert len(done) == 2
    meta["privacy"] = "public"
    save_json(new / "meta.json", meta)
    done2 = series.flush(yt=FakeYT())
    assert len(done2) == 1 and comments[-1][0] == "B" and "Previous part: https://youtube.com/shorts/A" in comments[-1][1]
    assert load_json(new / "meta.json")["series_pending"] == []


def test_series_flush_keeps_failed_actions_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(series, "OUTPUT_DIR", tmp_path)
    d = _short(tmp_path, "2026-10-24T10-00-00Z_b", series_name="x", youtube_id="B", privacy="public",
               series_pending=[{"type": "description", "video": "A", "dir": "nope", "append": "Next part: u", "attempts": 0}])

    def boom(*a, **k):
        raise RuntimeError("quota")
    monkeypatch.setattr(series, "_append_description", boom)
    assert series.flush() == []
    pend = load_json(d / "meta.json")["series_pending"]
    assert len(pend) == 1 and pend[0]["attempts"] == 1 and "quota" in pend[0]["error"]


# ---------------------------------------------------------------------------------------------------- compilations
def test_compilation_picks_the_busiest_topic_and_its_best_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(compilation, "OUTPUT_DIR", tmp_path)
    now = time.time()
    for i, vph in enumerate([5, 1, 9, 3, 7]):
        d = _short(tmp_path, f"2026-10-2{i}T10-00-00Z_h{i}", topic="history", youtube_id=f"H{i}", privacy="public",
                   uploaded_at=now - i * 86400, channel_stats={"views": vph * 10, "views_per_hour": vph})
        (d / "short.mp4").write_bytes(b"x")
    for i in range(2):
        d = _short(tmp_path, f"2026-10-2{i}T12-00-00Z_s{i}", topic="science", youtube_id=f"S{i}", privacy="public", uploaded_at=now)
        (d / "short.mp4").write_bytes(b"x")
    old = _short(tmp_path, "2026-09-01T10-00-00Z_old", topic="history", youtube_id="OLD", privacy="public", uploaded_at=now - 30 * 86400)
    (old / "short.mp4").write_bytes(b"x")
    gone = _short(tmp_path, "2026-10-25T10-00-00Z_gone", topic="history", youtube_id="G", privacy="public", uploaded_at=now)  # no video file
    cands = compilation.candidates(now=now, window_days=7)
    assert {c["youtube_id"] for c in cands} == {"H0", "H1", "H2", "H3", "H4", "S0", "S1"}
    cfg = {**compilation.DEFAULTS, "min_parts": 4, "max_parts": 4}
    choice = compilation.pick(cands, cfg)
    assert choice["label"] == "history" and [p["youtube_id"] for p in choice["parts"]] == ["H2", "H4", "H0", "H3"]
    assert compilation.pick(cands, cfg, topic="science") is None                   # only 2 science Shorts this week
    assert compilation.pick(cands, {**cfg, "min_parts": 2}, topic="science")["label"] == "science"


def test_cleanup_keeps_recent_well_watched_shorts_for_the_compilation(tmp_path, monkeypatch):
    monkeypatch.setattr("shorts_pipeline.config.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(compilation, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(compilation, "config", lambda: {**compilation.DEFAULTS, "enabled": True, "window_days": 7})
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: {"clean_after_upload": True, "keep_video_days": 0,
                                                                  "keep_unuploaded_days": 30})
    now = time.time()
    rates = [10, 8, 6, 4, 2, 1]
    dirs = []
    for i, vph in enumerate(rates):
        d = _short(tmp_path, f"2026-10-2{i}T10-00-00Z_v{i}", youtube_id=f"V{i}", privacy="public", uploaded_at=now - 3600 * (i + 1),
                   channel_stats={"views": 100, "views_per_hour": vph})
        (d / "short.mp4").write_bytes(b"x" * 10)
        dirs.append(d)
    unknown = _short(tmp_path, "2026-10-27T10-00-00Z_u", youtube_id="U", privacy="public", uploaded_at=now - 60)
    (unknown / "short.mp4").write_bytes(b"x")
    compiled = _short(tmp_path, "2026-10-27T11-00-00Z_c", youtube_id="C", privacy="public", uploaded_at=now - 60, compiled_in="x",
                      channel_stats={"views_per_hour": 50})
    (compiled / "short.mp4").write_bytes(b"x")
    compilation._MEDIAN_CACHE.update(at=0.0)
    housekeeping.cleanup_outputs(now=now)
    # median views/hour is 5; 0.8 x 5 = 4: the Shorts at 10, 8, 6, 4 keep their video, 2 and 1 lose it
    kept = [(d / "short.mp4").exists() for d in dirs]
    assert kept == [True, True, True, True, False, False]
    assert (unknown / "short.mp4").exists()                 # no stats yet: kept
    assert not (compiled / "short.mp4").exists()            # already in a compilation: gone
    housekeeping.cleanup_outputs(now=now, force_uploaded=True)
    assert not any((d / "short.mp4").exists() for d in dirs)   # low disk overrides the keep rule


def test_compilation_due_once_a_week(tmp_path, monkeypatch):
    monkeypatch.setattr(compilation, "STATE_PATH", tmp_path / "comp.json")
    monkeypatch.setattr(compilation, "config", lambda: {**compilation.DEFAULTS, "weekday": 6, "hour": 11})
    sunday_morning = datetime(2026, 11, 1, 9, 0)
    sunday_noon = datetime(2026, 11, 1, 12, 0)
    assert not compilation.due(sunday_morning) and compilation.due(sunday_noon)
    assert not compilation.due(datetime(2026, 11, 2, 12, 0))          # Monday
    submitted = []
    assert compilation.maybe_weekly(sunday_noon, lambda kind, params: submitted.append("never") or None, ready=lambda: False) is None
    assert compilation.due(sunday_noon) and submitted == []           # nothing to compile: not submitted, still due
    assert compilation.maybe_weekly(sunday_noon, lambda kind, params: {"id": "x"}, ready=lambda: True) is None   # checked < 1 h ago
    save_json(compilation.STATE_PATH, {})
    assert compilation.maybe_weekly(sunday_noon, lambda kind, params: submitted.append(kind) or None, ready=lambda: True) is None
    assert compilation.due(sunday_noon)                                # busy machine: still due next tick
    job = compilation.maybe_weekly(sunday_noon, lambda kind, params: {"id": "j1"}, ready=lambda: True)
    assert job["id"] == "j1" and submitted == ["compile"]
    assert not compilation.due(sunday_noon) and not compilation.due(datetime(2026, 11, 1, 23, 0))
    assert compilation.due(datetime(2026, 11, 8, 12, 0))               # next Sunday


def _have_ffmpeg() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not installed")
def test_compilation_build_stitches_shorts_into_landscape_with_chapters(tmp_path):
    from shorts_pipeline.render import probe_duration
    parts = []
    for i, colour in enumerate(("red", "blue")):
        p = tmp_path / f"s{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=180x320:d=1.2:r=30",
                        "-f", "lavfi", "-t", "1.2", "-i", "sine=frequency=440:sample_rate=24000",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
                       check=True, timeout=120)
        parts.append({"path": p, "title": f"Short {i + 1}", "youtube_id": f"V{i}"})
    video, chapters, total = compilation.build(parts, tmp_path / "out", card_seconds=0.5, style=None)
    assert video.exists() and len(chapters) == 2 and chapters[0][0] == 0.0 and 1.5 <= chapters[1][0] <= 1.9
    assert 3.2 <= total <= 3.6 and abs(probe_duration(video) - total) < 0.5
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(video)], capture_output=True, text=True, timeout=30).stdout.strip()
    assert probe.replace("\r", "") == "1920,1080"


def test_compilation_description_and_hashtags():
    parts = [{"title": "A", "youtube_id": "a1", "music": {"file": "x.mp3", "title": "Calm", "creator": "Kevin", "license": "CC BY"},
              "hashtags": ["history", "facts"], "photo_credits": []},
             {"title": "B", "youtube_id": "b2", "music": None, "hashtags": ["history", "shorts"], "photo_credits": []}]
    desc = compilation.description_for("Two stories.", [(0.0, "A"), (61.0, "B")], parts)
    assert "0:00 A" in desc and "1:01 B" in desc and "youtube.com/shorts/b2" in desc and desc.startswith("Two stories.")
    tags = compilation.hashtags_for(parts, "history")
    assert tags[0] == "history" and "compilation" in tags and "shorts" not in tags


# ---------------------------------------------------------------------------------------------------- fact-check on your own scripts
def test_fact_check_mode_for_own_scripts_and_review_rules(monkeypatch):
    monkeypatch.setattr(factcheck, "config", lambda: {**factcheck.DEFAULTS, "custom": "review"})
    assert factcheck.mode_for({"backend": "claude_code"}) == "fix"
    assert factcheck.mode_for({"backend": "custom"}) == "review"
    assert factcheck.mode_for({"backend": "claude_code", "original_text": "my words"}) == "review"   # enhanced script
    assert factcheck.mode_for({"backend": "claude_code"}, user_written=True) == "review"
    assert factcheck.mode_for({"backend": "dub"}) == "off"
    wrong = {"claim": "Rome fell in 576", "verdict": "contradicted", "correction": "476", "core": True}
    result = {"claims": [wrong, {"claim": "ok", "verdict": "supported"}], "supported": 1, "contradicted": [wrong], "unverifiable": [], "sources": []}
    assert factcheck.needs_review(result, "review")                    # wrong and not corrected: wait for the owner
    assert "1 wrong" in factcheck.summary(result)
    assert "WRONG: Rome fell in 576 -> 476" in factcheck.doubts_text(result)
    fixed = {**result, "fixed": True}
    assert not factcheck.needs_review(fixed, "fix") and "1 corrected" in factcheck.summary(fixed)
    monkeypatch.setattr(factcheck, "config", lambda: {**factcheck.DEFAULTS, "custom": "off", "enabled": True})
    assert factcheck.mode_for({"backend": "custom"}) == "off"


def test_fact_check_ambiguous_evidence_is_not_a_correction(monkeypatch):
    import typesafe_sdk

    class _A:
        def __init__(self, v): self.noul = v

    class _R:
        def __init__(self, answers): self.answers = answers

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def system_one(self, state, questions):
            return _R({"sup_0": _A(0.8), "con_0": _A(0.7), "sup_1": _A(0.1), "con_1": _A(0.9)})
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _C)
    monkeypatch.setattr(factcheck, "has_typesafe", lambda: True)
    claims = [{"claim": "the fat closed over the handle", "verdict": "supported", "evidence": "the fat closed upon the blade", "correction": "", "core": True},
              {"claim": "Rome fell in 576", "verdict": "contradicted", "evidence": "Rome fell in 476", "correction": "476", "core": True}]
    factcheck._verify_with_typesafe(claims)
    assert claims[0]["verdict"] == "unverifiable" and claims[0]["note"] == "evidence ambiguous"
    assert claims[1]["verdict"] == "contradicted"
