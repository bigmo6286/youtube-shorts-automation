"""TypeSafe out of credits (402): a clean stop, slots skipped while it lasts, and a resume once credits are back."""
import sys
import types
from datetime import datetime

import pytest

from shorts_pipeline import alerts, scheduler, usage

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
