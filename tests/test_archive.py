"""Uploaded Shorts shed the queue's fields at once and leave output/ (and the console page) once settled."""
import time

from shorts_pipeline import folders, housekeeping, retention, series
from shorts_pipeline.storage import load_json, save_json

DAY = 86400


def _short(root, name, files=("meta.json", "script.json", "thumbnail.jpg"), **meta):
    d = root / name
    d.mkdir(parents=True)
    for f in files:
        if f == "meta.json":
            save_json(d / f, {"title": name, **meta})
        elif f == "script.json":
            save_json(d / f, {"full_text": "words", "hook": "hook"})
        else:
            (d / f).write_bytes(b"x" * 10)
    return d


def test_settle_drops_queue_fields_and_the_caption_file(tmp_path):
    d = _short(tmp_path, "a", files=("meta.json", "captions.srt"), youtube_id="v1", upload_state="review",
               queued_at=1.0, priority_parts={"x": 1}, upload_priority=70, upload_error="boom", approval="pending",
               review_reason="fact-check", expired_at=2.0)
    meta = housekeeping.settle_uploaded(d)
    saved = load_json(d / "meta.json")
    assert saved == meta and saved["upload_state"] == "uploaded" and saved["upload_priority"] == 70
    assert not set(housekeeping.QUEUE_FIELDS) & set(saved)
    assert not (d / "captions.srt").exists()
    nd = _short(tmp_path, "b", files=("meta.json", "captions.srt"), upload_state="queued", queued_at=1.0)
    housekeeping.settle_uploaded(nd)                                        # not on YouTube: untouched
    assert load_json(nd / "meta.json")["queued_at"] == 1.0 and (nd / "captions.srt").exists()


def test_settled_shorts_move_to_the_archive_and_stay_readable(tmp_path, monkeypatch):
    out = tmp_path / "output"
    now = time.time()
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: {**housekeeping.CLEANUP_DEFAULTS, "archive_after_days": 7})
    _short(out, "old_public", youtube_id="v1", privacy="public", uploaded_at=now - 9 * DAY)
    _short(out, "new_public", youtube_id="v2", privacy="public", uploaded_at=now - 2 * DAY)
    _short(out, "old_private", youtube_id="v3", privacy="private", uploaded_at=now - 30 * DAY)
    _short(out, "old_with_video", files=("meta.json", "short.mp4"), youtube_id="v4", privacy="public", uploaded_at=now - 30 * DAY)
    _short(out, "dead_draft", upload_state="expired", video_removed=now - 8 * DAY)
    _short(out, "queued", files=("meta.json", "short.mp4"), upload_state="queued")
    assert housekeeping.archive_settled(dry_run=True, now=now, output_dir=out) == ["dead_draft", "old_public"]
    assert (out / "old_public").exists()                                    # a dry run moves nothing
    moved = housekeeping.archive_settled(now=now, output_dir=out)
    assert moved == ["dead_draft", "old_public"]
    arc = folders.archive_dir(out)
    assert sorted(p.name for p in (arc / "old_public").iterdir()) == ["meta.json", "script.json"]
    assert load_json(arc / "old_public" / "meta.json")["archived_at"] == now
    assert sorted(p.name for p in out.iterdir()) == ["new_public", "old_private", "old_with_video", "queued"]
    names = sorted(d.name for d in folders.short_dirs(out))
    assert names == ["dead_draft", "new_public", "old_private", "old_public", "old_with_video", "queued"]
    assert folders.find("old_public", out) == arc / "old_public" and folders.find("new_public", out) == out / "new_public"
    # the learners still see archived uploads
    monkeypatch.setattr(retention, "OUTPUT_DIR", out)
    assert "v1" in retention._outputs_by_video()


def test_series_parts_span_output_and_archive(tmp_path):
    out = tmp_path / "output"
    bp = {"key": "series:octopus", "series": "Octopus"}
    _short(out, "p2", youtube_id="v2", privacy="public", blueprint=bp, series_part=2)
    _short(folders.archive_dir(out), "p1", youtube_id="v1", privacy="public", blueprint=bp, series_part=1)
    assert [p["youtube_id"] for p in series.parts("Octopus", out)] == ["v1", "v2"]


def test_cleanup_settles_old_uploads_and_reports_the_archive(tmp_path, monkeypatch):
    monkeypatch.setattr("shorts_pipeline.config.OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: dict(housekeeping.CLEANUP_DEFAULTS))
    now = time.time()
    d = _short(tmp_path / "output", "2026-10-01T10-00-00Z_x", files=("meta.json", "short.mp4", "captions.srt"),
               youtube_id="v1", privacy="public", upload_state="uploaded", uploaded_at=now - 20 * DAY, queued_at=1.0)
    s = housekeeping.cleanup_outputs(now=now)
    assert s["videos_removed"] == 1 and s["archived"] == 1 and not d.exists()
    meta = load_json(folders.archive_dir(tmp_path / "output") / d.name / "meta.json")
    assert "queued_at" not in meta and meta["video_removed"] == now


def test_cleanup_removes_abandoned_half_made_folders(tmp_path, monkeypatch):
    import os
    out = tmp_path / "output"
    monkeypatch.setattr("shorts_pipeline.config.OUTPUT_DIR", out)
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: dict(housekeeping.CLEANUP_DEFAULTS))
    now = time.time()
    old = _short(out, "2026-10-08T12-12-05Z_stopped", files=("script.json", "voice.mp3", "bg_1.mp4"))
    fresh = _short(out, "2026-11-03T12-00-00Z_running", files=("script.json", "voice.mp3"))
    for f in list(old.iterdir()) + [old]:
        os.utime(f, (now - 3 * DAY, now - 3 * DAY))
    s = housekeeping.cleanup_outputs(now=now)
    assert s["half_made"] == 1 and not old.exists() and fresh.exists()       # a production in progress is left alone


def test_a_passed_publish_time_counts_as_public(tmp_path, monkeypatch):
    out = tmp_path / "output"
    now = time.time()
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: {**housekeeping.CLEANUP_DEFAULTS, "archive_after_days": 3})
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 4 * DAY))
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + DAY))
    _short(out, "went_public", youtube_id="v1", privacy="private", publish_at=past, uploaded_at=now - 5 * DAY)
    _short(out, "still_planned", youtube_id="v2", privacy="private", publish_at=future, uploaded_at=now - 5 * DAY)
    assert folders.live_privacy({"youtube_id": "v1", "privacy": "private", "publish_at": past}, now) == "public"
    assert folders.live_privacy({"youtube_id": "v2", "privacy": "private", "publish_at": future}, now) == "private"
    assert folders.live_privacy({"youtube_id": "v3", "privacy": "private"}, now) == "private"
    assert housekeeping.archive_settled(now=now, output_dir=out) == ["went_public"]


def test_privacy_refresh_is_not_blocked_by_a_production(monkeypatch):
    from types import SimpleNamespace
    from shorts_pipeline import upload
    from shorts_pipeline.web import server
    calls = []
    monkeypatch.setattr(upload, "oauth_available", lambda: True)
    monkeypatch.setattr(upload, "TOKEN_PATH", SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr(upload, "sync_output_status", lambda d: calls.append(d) or 0)
    monkeypatch.setattr(server, "_STATUS_SYNC", {"last": 0.0, "running": False, "error": None})
    monkeypatch.setattr(server, "_CURRENT", SimpleNamespace(kind="publish"))
    assert server._sync_youtube_status(force=True) is False                # a publish changes privacy itself
    monkeypatch.setattr(server, "_CURRENT", SimpleNamespace(kind="produce"))
    assert server._sync_youtube_status(force=True) is True
    for _ in range(100):
        if not server._STATUS_SYNC["running"]:
            break
        time.sleep(0.02)
    assert len(calls) == 1


def test_an_old_channel_list_does_not_undo_a_newer_privacy(tmp_path, monkeypatch):
    from shorts_pipeline import channel
    out = tmp_path / "output"
    monkeypatch.setattr(channel, "OUTPUT_DIR", out)
    now = time.time()
    d = _short(out, "2026-11-01T10-00-00Z_x", youtube_id="v1", privacy="public", privacy_checked_at=now,
               channel_stats={"views": 900, "seen_at": now}, duration=30)
    video = {"id": "v1", "title": "x", "privacy": "private", "views": 10, "likes": 0, "comments": 0,
             "views_per_hour": 1.0, "age_hours": 5, "published": "2026-11-01T10:00:00Z", "duration": 30}
    stale = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 14 * 3600))
    channel.match_outputs([video], stale)
    meta = load_json(d / "meta.json")
    assert meta["privacy"] == "public" and meta["channel_stats"]["views"] == 900       # the cache is 14 h old
    channel.match_outputs([{**video, "views": 1200}])                                  # a fresh read wins
    meta = load_json(d / "meta.json")
    assert meta["privacy"] == "private" and meta["channel_stats"]["views"] == 1200
