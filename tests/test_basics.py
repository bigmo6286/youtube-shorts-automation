"""Small pure functions: error classification, captions, storage, schema limits, repeat overlap, channel paths."""
import json
import os
import subprocess
import sys
from pathlib import Path

from shorts_pipeline import alerts, fetch, history, ollama_backend, storage

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ alerts.classify (real error texts from the logs)
def test_classify_known_errors():
    cases = {
        "RefreshError: ('invalid_grant: Token has been expired or revoked.')": ("youtube_token", False),
        'ResumableUploadError: "The user has exceeded the number of videos they may upload." uploadLimitExceeded': ("upload_limit", False),
        "ffmpeg failed (exit 4294967268): Error submitting a packet to the muxer: No space left on device": ("disk", False),
        "ServerNotFoundError: Unable to find the server at youtube.googleapis.com": ("network", True),
        "TimeoutError: [WinError 10060] A connection attempt failed": ("network", True),
        "NoAudioReceived: No audio was received.": ("voice", True),
        "RuntimeError: ffmpeg failed (exit 4294967274): Could not open encoder before EOF": ("render", True),
        "No script backend: set ANTHROPIC_API_KEY, or run `claude setup-token`": ("script_backend", False),
        "free local writer: no draft passed the quality checks (hook)": ("local_writer", True),
        "all 4 drafts repeated subjects that were already made; nothing produced": ("repeats", True),
    }
    for text, (key, retry) in cases.items():
        p = alerts.classify(text)
        assert (p.key, p.retryable) == (key, retry), text


def test_classify_unknown_error_is_not_retried():
    p = alerts.classify("KeyError: 'blueprint'")
    assert p.key.startswith("other:") and not p.retryable


# ------------------------------------------------------------------ captions: lines must not glue together
def test_caption_lines_are_joined_with_spaces():
    data = {"events": [{"segs": [{"utf8": "The"}, {"utf8": " moon"}, {"utf8": " drifts"}, {"utf8": " and"}]},
                       {"segs": [{"utf8": "\n"}]},
                       {"segs": [{"utf8": "it's"}, {"utf8": " leaving."}]}]}
    assert fetch.join_caption_events(data) == "The moon drifts and it's leaving."


# ------------------------------------------------------------------ storage: atomic writes, tolerant reads
def test_save_json_is_atomic_and_load_tolerates_empty_files(tmp_path):
    p = tmp_path / "x.json"
    storage.save_json(p, {"a": 1})
    assert storage.load_json(p) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))
    p.write_text("")                                   # what a full disk used to leave behind
    assert storage.load_json(p, {"default": True}) == {"default": True}


# ------------------------------------------------------------------ ollama schema: keep the title, add limits
def test_ollama_schema_keeps_title_and_limits():
    from shorts_pipeline.script_gen import ShortScript
    sc = ollama_backend._inline_refs(ShortScript.model_json_schema(), ollama_backend.LIMITS)
    assert "title" in sc["properties"] and "title" in sc["required"]
    assert sc["properties"]["title"]["maxLength"] == ollama_backend.LIMITS["title"]
    assert (sc["properties"]["lines"]["minItems"], sc["properties"]["lines"]["maxItems"]) == ollama_backend.LIMITS["lines"]
    assert "$defs" not in json.dumps(sc)


def test_reasoning_models_answer_without_thinking():
    assert ollama_backend._thinks("qwen3.5:9b") and ollama_backend._thinks("deepseek-r1:8b")
    assert not ollama_backend._thinks("qwen2.5:3b") and not ollama_backend._thinks("gemma4:e4b")


# ------------------------------------------------------------------ repeat check without TypeSafe (word overlap)
def test_repeat_check_by_overlap(monkeypatch):
    monkeypatch.setattr("shorts_pipeline.judge.has_typesafe", lambda: False)
    known = [{"title": "A Lost Dog Walked 2,500 Miles Home to Oregon in 1923", "hook": ""},
             {"title": "This Frog Freezes Solid Every Winter and Its Heart Stops", "hook": ""}]
    assert history.find_repeat({"title": "The Lost Dog Who Walked 2,500 Miles Home in 1923", "hook": ""}, known)
    assert history.find_repeat({"title": "The Mantis Shrimp Punches as Fast as a Bullet", "hook": ""}, known) is None


# ------------------------------------------------------------------ channel workspaces
def test_extra_channel_has_its_own_folders_and_shares_caches():
    code = ("from shorts_pipeline import config, storage, upload;"
            "print(config.DATA_DIR.relative_to(config.ROOT).as_posix(), storage.CACHE_DIR.relative_to(config.ROOT).as_posix(),"
            " upload.TOKEN_PATH.relative_to(config.ROOT).as_posix())")
    env = dict(os.environ, SHORTS_CHANNEL="pytest-other")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == ["channels/pytest-other/data", "data/cache", "channels/pytest-other/data/youtube_token.json"]


def test_cli_parser_knows_every_command():
    from shorts_pipeline.cli import build_parser
    p = build_parser()
    for argv in (["produce", "--blueprint-key", "a|b"], ["upload", "output/x", "--force"], ["cleanup", "--dry-run"],
                 ["channels", "add", "second", "--label", "Two"], ["channel", "connect-analytics"], ["report", "--send"],
                 ["queue"], ["web", "--port", "8790"]):
        assert p.parse_args(argv).func
