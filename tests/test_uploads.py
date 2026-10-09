"""Uploads: resume after network drops, never retry limits; the queue learns the limit and counts attempts;
publish times respect the review window, the per-hour cap and the gap."""
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

from shorts_pipeline import publish_times, upload, upload_queue
from shorts_pipeline.storage import load_json, save_json


def _http_error(status, text):
    return HttpError(httplib2.Response({"status": status}), text.encode())


class _Progress:
    def __init__(self, p):
        self.p = p

    def progress(self):
        return self.p


class _Request:
    def __init__(self, steps):
        self.steps, self.calls = list(steps), 0

    def next_chunk(self):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture
def fake_youtube(monkeypatch, no_sleep):
    holder = {}

    class _YT:
        def videos(self):
            return self

        def insert(self, **kw):
            holder["body"] = kw["body"]
            return holder["request"]

    monkeypatch.setattr(upload, "youtube_service", lambda interactive=True: _YT())
    monkeypatch.setattr("googleapiclient.http.MediaFileUpload", lambda *a, **k: None)
    return holder


def _upload(holder, steps, **kw):
    holder["request"] = _Request(steps)
    return upload.upload_video(Path("x.mp4"), title="t", description="d", tags=[], **kw)


def test_upload_resumes_after_network_drops(fake_youtube):
    r = _upload(fake_youtube, [(_Progress(.3), None), TimeoutError("timed out"),
                               httplib2.ServerNotFoundError("Unable to find the server"), (None, {"id": "abc"})])
    assert r == {"id": "abc"} and fake_youtube["request"].calls == 4


def test_upload_retries_google_5xx_but_not_the_upload_limit(fake_youtube):
    assert _upload(fake_youtube, [_http_error(503, "backendError"), (None, {"id": "x"})]) == {"id": "x"}
    with pytest.raises(HttpError):
        _upload(fake_youtube, [_http_error(400, "uploadLimitExceeded")])
    assert fake_youtube["request"].calls == 1


def test_upload_gives_up_when_the_network_stays_down(fake_youtube):
    with pytest.raises(socket.gaierror):
        _upload(fake_youtube, [socket.gaierror("getaddrinfo failed")] * 20)


def test_scheduled_publish_sets_publish_at_and_private(fake_youtube):
    _upload(fake_youtube, [(None, {"id": "x"})], privacy="public", publish_at="2026-10-10T09:00:00.000Z")
    assert fake_youtube["body"]["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                                              "publishAt": "2026-10-10T09:00:00.000Z"}


# ------------------------------------------------------------------ queue
@pytest.fixture
def queue_state(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_queue, "STATE_PATH", tmp_path / "upload_queue.json")
    save_json(tmp_path / "upload_queue.json", {"uploads": []})
    return tmp_path


def test_limit_error_learns_the_limit_and_pauses(queue_state):
    for _ in range(5):
        upload_queue.record_upload()
    note = upload_queue.on_upload_error(queue_state, "uploadLimitExceeded: The user has exceeded the number of videos")
    assert upload_queue.limit() == 5 and upload_queue.paused() and "paused" in note.lower()


def test_network_failures_give_up_after_three_attempts(queue_state):
    save_json(queue_state / "meta.json", {"title": "t", "upload_state": "queued"})
    for _ in range(3):
        upload_queue.on_upload_error(queue_state, "TimeoutError: timed out")
    meta = load_json(queue_state / "meta.json")
    assert meta["upload_attempts"] == 3 and meta["upload_state"] == "failed"


def test_next_upload_picks_the_best_above_the_minimum(queue_state, monkeypatch):
    token = queue_state / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(upload, "TOKEN_PATH", token)
    monkeypatch.setattr(upload_queue, "queued", lambda: [
        {"dir": "a", "title": "A", "priority": 81, "queued_at": 1}, {"dir": "b", "title": "B", "priority": 64, "queued_at": 2},
        {"dir": "c", "title": "C", "priority": 12, "queued_at": 3}])
    item, _ = upload_queue.next_upload()
    assert item["dir"] == "a"
    upload_queue.mark_started()
    item, reason = upload_queue.next_upload()
    assert item is None and "spacing" in reason


# ------------------------------------------------------------------ publish times
def test_publish_time_respects_review_window_cap_and_gap(monkeypatch):
    scores = [0.1] * 24
    scores[3] = 1.0                                          # 03:00 UTC is clearly the best hour
    monkeypatch.setattr(publish_times, "hour_scores", lambda: {"scores": scores, "videos": [0] * 24, "overall": .1, "n": 0})
    taken = []
    monkeypatch.setattr(publish_times, "_taken", lambda: list(taken))
    now = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
    for _ in range(3):
        taken.append(publish_times.choose(now))
    assert all(t >= now + timedelta(hours=2) for t in taken)               # review window
    assert all(t <= now + timedelta(hours=2 + 24 + 1) for t in taken)      # never waits past the horizon
    first_day = sorted(t for t in taken if t.date() == now.date() and t.hour == 3)
    assert len(first_day) == 2                                              # at most 2 in the best hour...
    assert (first_day[1] - first_day[0]) >= timedelta(minutes=25)           # ...spaced apart
    assert sorted(taken)[-1].hour == 3 and sorted(taken)[-1].date() > now.date()   # the third waits for the next best slot
