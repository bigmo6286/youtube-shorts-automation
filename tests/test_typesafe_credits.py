"""TypeSafe out of credits (402): a clean stop, slots skipped while it lasts, and a resume once credits are back."""
import sys
import types
from datetime import datetime

import pytest

from shorts_pipeline import alerts, claude_judge, scheduler, usage

MSG = ("POST https://api.typesafe.ai/v1/systemone: 402 Your organization has no available TypeSafe API credits. "
       "Please add more credits and/or set up auto-reload")


@pytest.fixture
def credits(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CREDITS_PATH", tmp_path / "typesafe_credits.json")
    answers = {"raise": MSG}
    calls = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def system_one(self, **kw):
            calls.append(kw)
            if answers["raise"]:
                raise RuntimeError(answers["raise"])
            return object()
    monkeypatch.setitem(sys.modules, "typesafe_sdk", types.SimpleNamespace(TypeSafeClient=FakeClient))
    monkeypatch.setattr(usage, "install", lambda: None)
    monkeypatch.setattr(usage, "_ORIGINAL", {})
    monkeypatch.setattr(claude_judge, "enabled", lambda: False)
    return answers, calls


def test_the_402_is_recognised_and_never_retried():
    assert usage.out_of_credits_error(RuntimeError(MSG))
    assert not usage.out_of_credits_error(RuntimeError("POST ...: 500 internal error"))
    p = alerts.classify(f"TypeSafeAPIError: {MSG}")
    assert p.key == "typesafe_credits" and not p.retryable
    assert alerts.classify(usage.OUT_OF_CREDITS).key == "typesafe_credits"


def test_nothing_is_asked_until_a_402_is_seen_then_once_an_hour(credits):
    answers, calls = credits
    assert usage.typesafe_out_of_credits(recheck_after=0) is False and calls == []      # never ran out: no call
    usage._note_credits(False)
    assert usage.typesafe_out_of_credits() is True and calls == []                      # just seen: no call
    assert usage.typesafe_out_of_credits(recheck_after=0) is True and len(calls) == 1   # still out
    answers["raise"] = None
    assert usage.typesafe_out_of_credits(recheck_after=0) is False                      # credits are back
    assert not usage.CREDITS_PATH.exists()
    assert usage.typesafe_out_of_credits(recheck_after=0) is False and len(calls) == 2


def test_a_network_failure_during_the_check_keeps_the_pause(credits):
    answers, _ = credits
    usage._note_credits(False)
    answers["raise"] = "Max retries exceeded: getaddrinfo failed"
    assert usage.typesafe_out_of_credits(recheck_after=0) is True and usage.CREDITS_PATH.exists()


def test_scheduled_slots_are_skipped_while_credits_are_out(credits, monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "schedule_state.json")
    sent = []
    monkeypatch.setattr(alerts, "send", lambda key, text, **kw: sent.append(key) or True)
    submitted = []
    s = scheduler.Scheduler(lambda kind, params: submitted.append(kind) or {"id": "x", "status": "queued"},
                            lambda job_id: None)
    usage._note_credits(False)
    assert s._fire("produce", 0, datetime(2026, 11, 9, 12, 18), {"source": "topic", "topics": ["cats"]}) is True
    assert submitted == [] and sent == ["typesafe_credits"]
    assert s.state["history"][-1]["kind"] == "produce (skipped: TypeSafe out of credits)"


def test_with_claude_standing_in_slots_go_on(credits, monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "schedule_state.json")
    monkeypatch.setattr(claude_judge, "enabled", lambda: True)
    usage._note_credits(False)
    assert usage.typesafe_out_of_credits() and not usage.typesafe_blocked()
    submitted = []
    s = scheduler.Scheduler(lambda kind, params: submitted.append(kind) or {"id": "x", "status": "queued"},
                            lambda job_id: None)
    s._fire("produce", 0, datetime(2026, 11, 9, 12, 18), {"source": "topic", "topics": ["cats"]})
    assert submitted == ["produce"]


def test_calls_go_to_claude_while_out_and_back_to_typesafe_after(credits, monkeypatch):
    monkeypatch.setattr(claude_judge, "enabled", lambda: True)
    monkeypatch.setattr(claude_judge, "answer", lambda state, questions, **kw: "claude")
    typesafe = []

    def original(client, state, questions, **kw):
        typesafe.append(1)
        if original.out:
            raise RuntimeError(MSG)
        return "typesafe"
    original.out = True
    ask = lambda: usage._system_one(original, None, ({"t": 1}, {"q": {"type": "noul"}}), {})   # noqa: E731
    assert ask() == "claude" and usage.CREDITS_PATH.exists() and len(typesafe) == 1    # the 402 is handed to Claude
    assert ask() == "claude" and len(typesafe) == 1                                    # then TypeSafe is not asked
    original.out = False
    usage._note_credits(True)                                                          # the hourly check: credits back
    assert ask() == "typesafe" and not usage.CREDITS_PATH.exists()


def test_claude_answers_come_back_as_typesafe_answers():
    qs = {"a": {"type": "noul", "instructions": "x"},
          "b": {"type": "choice", "instructions": "y", "criteria": {"cat": "c", "dog": None}},
          "c": {"type": "score", "instructions": "z", "criteria": ["low", "mid", "high"]}}
    r = claude_judge.to_response(qs, {"a": 0.8, "b": {"cat": 0.3, "dog": 0.9}, "c": [0.1, 0.2, 0.7]})
    assert r.answers["a"].noul == 0.8 and r.nouls.keys() == {"a"}
    assert r.answers["b"].choice == "dog" and abs(r.answers["b"].probabilities["dog"] - 0.75) < 1e-9
    assert abs(r.answers["c"].score - 1.6) < 1e-9 and r.answers["c"].probabilities[2] == 0.7
    with pytest.raises(ValueError):
        claude_judge._schema({"x": {"type": "box"}})
