"""Nightly backup, Telegram commands, and dubbing."""
import zipfile

import pytest

from shorts_pipeline import backup, dub, telegram_bot, tts
from shorts_pipeline.storage import load_json, save_json


# ------------------------------------------------------------------ backup
def test_backup_round_trip_never_contains_secrets(tmp_path, monkeypatch):
    data, out, shared = tmp_path / "data", tmp_path / "out", tmp_path / "shared"
    for d in (data, out / "short1", shared / "cache"):
        d.mkdir(parents=True)
    save_json(data / "produced_titles.json", {"videos": [{"title": "T"}]})
    save_json(data / "youtube_token.json", {"token": "SECRET"})
    save_json(out / "short1" / "meta.json", {"title": "T"})
    for name, value in (("DATA_DIR", data), ("OUTPUT_DIR", out), ("SHARED_DIR", shared),
                        ("LOCAL_CONFIG", tmp_path / "config.local.yaml")):
        monkeypatch.setattr(backup, name, value)
    monkeypatch.setattr(backup, "config", lambda: dict(backup.DEFAULTS, folder=str(tmp_path / "bk"), keep=2))
    z = backup.make()
    names = zipfile.ZipFile(z).namelist()
    assert "data/produced_titles.json" in names and "output/short1/meta.json" in names
    assert not any("token" in n for n in names)
    (data / "produced_titles.json").unlink()
    assert backup.restore(z) >= 2 and load_json(data / "produced_titles.json")["videos"][0]["title"] == "T"
    assert load_json(data / "youtube_token.json")["token"] == "SECRET"       # untouched by a restore


# ------------------------------------------------------------------ Telegram commands
def test_bot_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_bot, "STATE_PATH", tmp_path / "bot.json")
    out = tmp_path / "out"
    (out / "a").mkdir(parents=True)
    save_json(out / "a" / "meta.json", {"youtube_id": "vid1", "title": "Coral Hides", "privacy": "private",
                                        "publish_at": "2026-10-10T02:00:00Z"})
    monkeypatch.setattr(telegram_bot, "OUTPUT_DIR", out)
    assert "/publish" in telegram_bot.handle("/help")
    assert "1. Coral Hides [goes public" in telegram_bot.handle("/uploads")
    changed = []
    monkeypatch.setattr("shorts_pipeline.upload.set_privacy", lambda vid, p: changed.append((vid, p)))
    assert "is now public" in telegram_bot.handle("/publish 1") and changed == [("vid1", "public")]
    assert load_json(out / "a" / "meta.json")["publish_at"] is None
    assert "first" in telegram_bot.handle("/publish 7")
    assert "Unknown" in telegram_bot.handle("/selfdestruct")


# ------------------------------------------------------------------ dubbing
def test_translation_must_keep_every_line(monkeypatch):
    monkeypatch.setattr("shorts_pipeline.script_gen.pick_backend", lambda pref: "claude_code")
    script = {"title": "T", "hook": "H.", "cta": "C", "description": "D", "hashtags": ["shorts"], "thumbnail_text": "X",
              "lines": [{"text": "One.", "visual_keyword": "one"}, {"text": "Two.", "visual_keyword": "two"}]}
    monkeypatch.setattr("shorts_pipeline.claude_code_backend.generate_json", lambda s, p, sc: {
        "title": "Tí", "hook": "Hó.", "cta": "Cé", "description": "Dé", "hashtags": ["shorts"], "thumbnail_text": "Xé",
        "lines": ["Uno."]})
    with pytest.raises(RuntimeError, match="lines"):
        dub.translate(script)
    monkeypatch.setattr("shorts_pipeline.claude_code_backend.generate_json", lambda s, p, sc: {
        "title": "Tí", "hook": "Hó.", "cta": "Cé", "description": "Dé", "hashtags": ["shorts"], "thumbnail_text": "Xé",
        "lines": ["Uno.", "Dos."]})
    t = dub.translate(script)
    assert [ln["visual_keyword"] for ln in t["lines"]] == ["one", "two"]           # footage terms stay English
    assert t["full_text"] == "Hó. Uno. Dos. Cé" and t["backend"] == "dub"


def test_english_fallback_voice_is_never_used_for_a_dub(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "config", lambda: {"engine": "edge", "fallback": "kokoro", "kokoro_voice": "am_michael"})
    monkeypatch.setattr(tts, "synthesize_edge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no audio")))
    monkeypatch.setattr(tts, "kokoro_available", lambda: True)
    monkeypatch.setattr(tts, "synthesize_kokoro", lambda *a, **k: [{"text": "x", "start": 0, "end": 1}])
    with pytest.raises(RuntimeError):
        tts.synthesize("Hola.", tmp_path / "v.mp3", voice="es-MX-JorgeNeural")
    assert tts.synthesize("Hi.", tmp_path / "v.mp3", voice="en-US-AndrewNeural")[0]["text"] == "x"
