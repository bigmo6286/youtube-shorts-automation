"""Scheduler retries, queue uploads, queue-full skips and trend-run reuse; script QA: repeats are rejected, the free
local writer is held to stricter rules, and a Claude failure mid-job switches to it."""
from datetime import datetime
from pathlib import Path

import pytest

import shorts_pipeline.scheduler as sch
import shorts_pipeline.script_gen as sg
import shorts_pipeline.upload_queue as uq


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(sch, "STATE_PATH", tmp_path / "schedule.json")
    cfg = dict(sch.DEFAULTS, enabled=True, produces_per_day=4, refresh_per_day=0, start_hour=8, end_hour=20)
    monkeypatch.setattr(sch, "schedule_config", lambda: dict(cfg))
    monkeypatch.setattr("shorts_pipeline.alerts.maybe_send_daily_report", lambda now=None: False)
    monkeypatch.setattr("shorts_pipeline.housekeeping.cleanup_outputs", lambda **k: {})
    monkeypatch.setattr(sch, "latest_run_dir", lambda: Path("fake-run"))
    real_load = sch.load_json
    bp = [{"format": "listicle_facts", "topic": "space_astronomy", "hook_style": "bold_claim", "opportunity": 1.0}]
    monkeypatch.setattr(sch, "load_json", lambda p, d=None: {"blueprints": bp} if str(p).endswith("analysis.json") else real_load(p, d))
    monkeypatch.setattr("shorts_pipeline.channel.blueprint_performance", lambda *a, **k: {})
    state = {"jobs": {}, "errors": {}, "submitted": [], "queue_item": None, "queue_full": False}
    monkeypatch.setattr(uq, "next_upload", lambda now=None: (state["queue_item"], "test"))
    monkeypatch.setattr(uq, "mark_started", lambda: None)
    monkeypatch.setattr(uq, "expire_old", lambda: 0)
    monkeypatch.setattr(sch.Scheduler, "_queue_full", lambda self: state["queue_full"])

    def submit(kind, params):
        jid = f"j{len(state['submitted']) + 1}"
        state["submitted"].append((kind, params))
        state["jobs"][jid] = "running"
        return {"id": jid, "status": "running"}

    s = sch.Scheduler(submit, lambda j: state["jobs"].get(j), lambda j: state["errors"].get(j))
    return s, state


def _at(h, m=0):
    return datetime(2026, 10, 9, h, m)


def test_temporary_failure_is_retried_once(scheduler):
    s, st = scheduler
    s.tick(_at(8, 1))
    st["jobs"]["j1"], st["errors"]["j1"] = "error", "ServerNotFoundError: Unable to find the server"
    s.tick(_at(8, 5))
    assert len(s.state["retries"]) == 1
    s.tick(_at(8, 40))
    assert [k for k, _ in st["submitted"]] == ["produce", "produce"]
    st["jobs"]["j2"], st["errors"]["j2"] = "error", "TimeoutError: timed out"
    s.tick(_at(8, 45))
    assert s.state["retries"] == []                                   # a failed retry is not retried again


def test_permanent_failure_is_not_retried(scheduler):
    s, st = scheduler
    s.tick(_at(8, 1))
    st["jobs"]["j1"], st["errors"]["j1"] = "error", "RefreshError: invalid_grant"
    s.tick(_at(8, 5))
    assert s.state.get("retries", []) == []


def test_queue_uploads_when_nothing_is_due_and_full_queue_skips_slots(scheduler):
    s, st = scheduler
    s.tick(_at(8, 1))
    st["jobs"]["j1"] = "done"
    st["queue_item"] = {"dir": "d1", "title": "T", "priority": 80}
    s.tick(_at(9, 0))
    assert st["submitted"][-1] == ("upload", {"path": "output/d1", "auto": True})
    st["queue_item"], st["queue_full"] = None, True
    n = len(st["submitted"])
    s.tick(_at(11, 1))                                              # the 11:00 slot
    assert len(st["submitted"]) == n and "skipped" in s.state["history"][-1]["kind"]


# ------------------------------------------------------------------ scripts
_DRAFT = sg.ShortScript.model_validate({
    "title": "A Volcano Full of Ice", "hook": "This volcano is full of ice.",
    "lines": [{"text": "It sits in Antarctica.", "visual_keyword": "volcano snow"}] * 6, "cta": "Did you know?",
    "description": "Volcano facts.", "hashtags": ["shorts", "facts"], "visual_fallback": "volcano", "thumbnail_text": "ICE VOLCANO"})
_BP = {"format": "listicle_facts", "topic": "nature_environment", "hook_style": "bold_claim"}


def _qa(hook=2.6, payoff=.9, risk=.1):
    return lambda *a, **k: {"hook_strength": {"score": hook}, "policy_risk": {"noul": risk}, "matches_format": {"noul": .9},
                            "has_payoff": {"noul": payoff}, "clarity": {"score": 1.8}}


@pytest.fixture
def writer(monkeypatch):
    calls = []

    def draft(client, system, prompt, backend=""):
        calls.append(backend)
        if backend == "claude_code":
            raise RuntimeError("Claude Code failed: API Error: Can't reach the API server")
        return _DRAFT
    monkeypatch.setattr(sg, "_draft", draft)
    monkeypatch.setattr(sg, "_ollama_fallback_ready", lambda: True)
    return calls


def _write(monkeypatch, backend, qa, repeat=None):
    monkeypatch.setattr(sg, "judge_script", qa)
    monkeypatch.setattr(sg, "pick_backend", lambda pref: backend)
    return sg._write_with_qa(system="s", user_prompt="p", blueprint=_BP, backend=backend, max_attempts=4,
                             min_hook_score=2.0, repeat_check=repeat)


def test_repeated_subject_is_never_returned(monkeypatch, writer):
    with pytest.raises(RuntimeError, match="repeated subjects"):
        _write(monkeypatch, "ollama", _qa(), repeat=lambda s: "An older video")


def test_local_writer_needs_every_check_to_pass(monkeypatch, writer):
    with pytest.raises(RuntimeError, match="free local writer"):
        _write(monkeypatch, "ollama", _qa(hook=1.0, payoff=.2))
    assert len(writer) == sg.LOCAL_MAX_ATTEMPTS
    with pytest.raises(RuntimeError, match="free local writer"):
        _write(monkeypatch, "ollama", _qa(risk=.45))                 # stricter accuracy limit than Claude's
    assert _write(monkeypatch, "ollama", _qa())["backend"] == "ollama"   # a clean local draft is accepted


def test_claude_failure_switches_to_the_local_writer(monkeypatch, writer):
    s = _write(monkeypatch, "claude_code", _qa())
    assert s["backend"] == "ollama" and writer[:2] == ["claude_code", "ollama"]
