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
