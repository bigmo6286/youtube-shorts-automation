"""A real ffmpeg render (captions, voice, outro card) and the output cleanup rules."""
import shutil
import subprocess
import time

import pytest

from shorts_pipeline import captions, housekeeping, render
from shorts_pipeline.storage import load_json, save_json
from shorts_pipeline.tools import ensure_ffmpeg_on_path

ensure_ffmpeg_on_path()
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _lavfi(out, source, seconds, extra=()):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", source, "-t", str(seconds), *extra, str(out)],
                   check=True)
    return out


@needs_ffmpeg
def test_render_makes_a_vertical_short_with_captions_and_outro(tmp_path):
    a = _lavfi(tmp_path / "a.mp4", "color=c=navy:s=640x360:r=30", 2, ["-pix_fmt", "yuv420p"])
    b = _lavfi(tmp_path / "b.mp4", "color=c=darkgreen:s=360x640:r=30", 2, ["-pix_fmt", "yuv420p"])
    voice = _lavfi(tmp_path / "voice.mp3", "sine=frequency=220:sample_rate=24000", 3)
    words = [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.45} for i, w in enumerate("this is a quick render test".split())]
    ass = captions.write_ass(words, tmp_path / "captions.ass", full_text="This is a quick render test.")
    outro_bg = _lavfi(tmp_path / "outro_bg.mp4", "color=c=black:s=1080x1920:r=30", 1, ["-pix_fmt", "yuv420p"])
    outro_ass = captions.write_card_ass("Follow for more", 1.0, tmp_path / "outro.ass")
    out = render.render([{"path": str(a), "start": 0, "end": 1.5}, {"path": str(b), "start": 1.5, "end": 3.0}],
                        voice, ass, tmp_path / "short.mp4", total_seconds=4.0,
                        outro={"path": str(outro_bg), "seconds": 1.0, "ass": str(outro_ass)})
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "1080,1920"
    assert 3.5 <= render.probe_duration(out) <= 4.6


def _folder(root, name, **meta):
    d = root / name
    d.mkdir()
    for f in ("short.mp4", "voice.mp3", "bg_0.mp4", "captions.ass", "words.json", "thumbnail.jpg", "script.json"):
        (d / f).write_bytes(b"x" * 100)
    save_json(d / "meta.json", meta)
    return d


def test_cleanup_rules(tmp_path, monkeypatch):
    monkeypatch.setattr("shorts_pipeline.config.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("shorts_pipeline.compilation.config", lambda: {"enabled": False, "window_days": 7})
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: {"clean_after_upload": True, "keep_video_days": 3,
                                                                  "keep_unuploaded_days": 30})
    now = time.time()
    fresh = _folder(tmp_path, "2026-10-09T10-00-00Z_a", youtube_id="A", privacy="public", uploaded_at=now - 3600)
    old = _folder(tmp_path, "2026-10-01T10-00-00Z_b", youtube_id="B", privacy="public", uploaded_at=now - 5 * 86400)
    waiting = _folder(tmp_path, "2026-10-08T10-00-00Z_c", upload_state="review")
    s = housekeeping.cleanup_outputs(now=now)
    assert not (fresh / "voice.mp3").exists() and (fresh / "short.mp4").exists()         # pieces go, video stays
    assert not (old / "short.mp4").exists() and load_json(old / "meta.json")["video_removed"]
    assert all((waiting / f).exists() for f in ("short.mp4", "voice.mp3"))                # never uploaded: untouched
    for d in (fresh, old, waiting):
        assert (d / "script.json").exists() and (d / "thumbnail.jpg").exists() and (d / "meta.json").exists()
    assert s["videos_removed"] == 1


# ------------------------------------------------------------------ console answers on a busy PC
def test_shared_answers_return_stale_at_once_and_refresh_in_background():
    import threading
    import time
    from shorts_pipeline.web import server
    server._forget_shared()
    calls = []
    release = threading.Event()

    def slow():
        calls.append(1)
        if len(calls) > 1:
            release.wait(5)
        return len(calls)

    assert server._shared("t", 0, slow) == 1                   # first answer: computed
    t0 = time.time()
    assert server._shared("t", 0, slow) == 1                   # stale answer at once, refresh started
    assert server._shared("t", 0, slow) == 1                   # no second refresh while one runs
    assert time.time() - t0 < 1 and len(calls) == 2
    release.set()
    for _ in range(50):
        if server._SHARED["t"][1] == 2:
            break
        time.sleep(0.05)
    assert server._shared("t", 60, slow) == 2
    server._forget_shared()


def test_uploaded_videos_go_at_the_next_cleanup_and_first_when_the_disk_is_low(tmp_path, monkeypatch):
    monkeypatch.setattr("shorts_pipeline.config.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("shorts_pipeline.compilation.config", lambda: {"enabled": False, "window_days": 7})
    now = time.time()
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: dict(housekeeping.CLEANUP_DEFAULTS))
    just = _folder(tmp_path, "2026-10-11T10-00-00Z_a", youtube_id="A", privacy="private", uploaded_at=now - 60)
    waiting = _folder(tmp_path, "2026-10-11T11-00-00Z_b", upload_state="queued")
    housekeeping.cleanup_outputs(now=now)
    assert not (just / "short.mp4").exists() and (just / "thumbnail.jpg").exists()       # default: gone at once
    assert (waiting / "short.mp4").exists()                                              # not on YouTube: kept
    # a user who keeps uploaded videos for a week still loses them first when the disk is low
    monkeypatch.setattr(housekeeping, "cleanup_config", lambda: {**housekeeping.CLEANUP_DEFAULTS, "keep_video_days": 7})
    kept = _folder(tmp_path, "2026-10-11T12-00-00Z_c", youtube_id="C", privacy="public", uploaded_at=now - 60)
    housekeeping.cleanup_outputs(now=now)
    assert (kept / "short.mp4").exists()
    monkeypatch.setattr(housekeeping, "free_gb", lambda path=None: 1.0 if (kept / "short.mp4").exists() else 5.0)
    monkeypatch.setattr(housekeeping, "prune_cache", lambda *a: pytest.fail("footage must not go first"))
    housekeeping.ensure_free_space()
    assert not (kept / "short.mp4").exists() and (waiting / "short.mp4").exists()


def test_delete_a_short_here_and_optionally_on_youtube(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from shorts_pipeline.web import server
    monkeypatch.setattr(server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(server, "_CURRENT", None)
    queued = _folder(tmp_path, "2026-10-16T12-00-00Z_a", upload_state="queued", title="Lion")
    uploaded = _folder(tmp_path, "2026-10-16T13-00-00Z_b", youtube_id="YT1", privacy="private", title="Bird")
    gone = []
    monkeypatch.setattr("shorts_pipeline.upload.delete_video", lambda vid: gone.append(vid))
    assert server.delete_output(queued.name) == {"deleted": queued.name, "youtube_deleted": False}
    assert not queued.exists() and gone == []
    assert server.delete_output(uploaded.name, youtube=True)["youtube_deleted"] and gone == ["YT1"]
    assert not uploaded.exists()
    for bad in ("..", "../x", "nope"):
        with pytest.raises(HTTPException):
            server.delete_output(bad)


def test_a_video_in_use_stops_the_delete_before_anything_is_removed(tmp_path, monkeypatch):
    from pathlib import Path
    from fastapi import HTTPException
    from shorts_pipeline.web import server
    monkeypatch.setattr(server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(server, "_CURRENT", None)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    d = _folder(tmp_path, "2026-10-16T13-42-34Z_long", title="Golden Gate")
    real_unlink = Path.unlink

    def locked(self, missing_ok=False):
        if self.name == "short.mp4":
            raise PermissionError("in use")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(HTTPException) as err:
        server.delete_output(d.name)
    assert err.value.status_code == 409
    assert (d / "meta.json").exists() and (d / "script.json").exists()         # the card survives
