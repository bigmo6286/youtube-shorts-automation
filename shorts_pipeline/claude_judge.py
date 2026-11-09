"""Claude answers TypeSafe's questions while TypeSafe is out of credits.

Every TypeSafe call in the engine goes through one wrapped `system_one` (usage.install). While the account is out of
credits and `typesafe.fallback` is `claude`, that call is answered here instead: the same state and questions go to
Claude (Claude Code, a fast model by default) and come back as the SDK's own answer objects (noul, choice and score
with their probabilities), so no caller changes. Images in the state are saved to files Claude opens with its Read
tool. Once an hour one tiny TypeSafe call checks the credits; when they are back, TypeSafe answers again.

Starting Claude Code takes 15-70 s on a slow PC while the answer itself takes 3-5 s, and a production asks 40-120
questions: so one Claude Code process stays open (stream-json in and out), each call is one message, and it is
restarted every MAX_TURNS messages (the conversation grows) and closed after IDLE_SECONDS without a question.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .config import load_config
from .storage import CACHE_DIR

log = logging.getLogger(__name__)

IMAGE_DIR = CACHE_DIR / "judge_images"
DEFAULTS = {"fallback": "claude", "fallback_model": "haiku"}

SYSTEM = (
    "You stand in for a judgment model. You are given a STATE (text, JSON, sometimes images) and named QUESTIONS. "
    "Answer each question about the state only, independently of the others, as a calibrated probability:\n"
    "- noul: the probability (0 to 1) that the answer is yes / the statement is true. 0.5 means truly undecided.\n"
    "- choice: a probability for every listed option, summing to 1.\n"
    "- score: a probability for every level (level 0 first), summing to 1.\n"
    "Be honest and decisive: put most of the probability on the answer you believe, keep some on close alternatives. "
    "Backticked paths in a question (like `script.hook`) refer to fields of the state.\n"
    "Every message is a new, independent task: ignore earlier messages. Reply with ONE JSON object and nothing else, "
    "no code fence, no commentary. Use the Read tool only to open image files a message names.")


def settings() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((load_config().get("typesafe") or {}))
    return cfg


def enabled() -> bool:
    """Claude stands in only when it is switched on and a Claude writer is installed on this machine."""
    if str(settings().get("fallback") or "off").lower() != "claude":
        return False
    try:
        from .claude_code_backend import find_claude_binary
        return bool(find_claude_binary())
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ questions and state as plain JSON
def _plain_question(q: Any) -> dict[str, Any]:
    if hasattr(q, "model_dump"):
        q = q.model_dump(exclude_none=True)
    q = dict(q)
    q.setdefault("type", "noul")
    return q


def _save_image(img: Any) -> str:
    data = getattr(img, "data", img)
    if isinstance(data, (str, Path)) or hasattr(data, "__fspath__"):
        return str(Path(data).resolve())
    raw = bytes(data)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".png" if raw[:4] == b"\x89PNG" else ".webp" if raw[8:12] == b"WEBP" else ".jpg"
    path = IMAGE_DIR / (hashlib.sha1(raw).hexdigest()[:16] + ext)
    if not path.exists():
        path.write_bytes(raw)
    return str(path.resolve())


def _plain_state(value: Any, images: list[str]) -> Any:
    """JSON for the prompt; every image becomes a file path Claude reads."""
    if type(value).__name__ in ("Image", "ResolvedImage"):
        path = _save_image(value)
        images.append(path)
        return f"[image file: {path}]"
    if isinstance(value, Mapping):
        return {str(k): _plain_state(v, images) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_state(v, images) for v in value]
    if hasattr(value, "model_dump"):
        return _plain_state(value.model_dump(exclude_none=True), images)
    return value


def _schema(questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name, q in questions.items():
        kind = q["type"]
        if kind == "noul":
            props[name] = {"type": "number", "minimum": 0, "maximum": 1}
        elif kind == "choice":
            labels = list((q.get("criteria") or {}).keys())
            props[name] = {"type": "object", "properties": {k: {"type": "number", "minimum": 0, "maximum": 1} for k in labels},
                           "required": labels, "additionalProperties": False}
        elif kind == "score":
            n = len(q.get("criteria") or [])
            props[name] = {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                           "minItems": n, "maxItems": n}
        else:
            raise ValueError(f"Claude cannot stand in for a {kind!r} question")
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _prompt(state: Any, questions: dict[str, dict[str, Any]], images: list[str]) -> str:
    lines = ["STATE:", json.dumps(state, ensure_ascii=False, indent=1, default=str)[:60000], "", "QUESTIONS:"]
    for name, q in questions.items():
        entry = {"type": q["type"], "instructions": q.get("instructions")}
        if q["type"] == "choice":
            entry["options"] = q.get("criteria")
        elif q["type"] == "score":
            entry["levels"] = {i: c for i, c in enumerate(q.get("criteria") or [])}
        elif q.get("criteria"):
            entry["criteria"] = q["criteria"]
        lines.append(f"{name}: " + json.dumps(entry, ensure_ascii=False, default=str))
    if images:
        lines += ["", "The state refers to image files. Open each one with the Read tool and look at it before you "
                      "answer; judge only what is actually visible: " + ", ".join(images)]
    lines += ["", "Return one JSON object with a key per question: a number for noul, an object of option -> "
                  "probability for choice, a list of level probabilities (level 0 first) for score."]
    return "\n".join(lines)


# ------------------------------------------------------------------ answers in the SDK's own types
def _normalise(ps: list[float]) -> list[float]:
    ps = [max(0.0, float(p or 0)) for p in ps]
    total = sum(ps)
    return [p / total for p in ps] if total > 0 else [1.0 / len(ps)] * len(ps)


def to_response(questions: dict[str, dict[str, Any]], raw: dict[str, Any]):
    from typesafe_sdk._core.response_types import (ChoiceAnswer, NoulAnswer, ScoreAnswer, SystemOneResponse,
                                                   Usage)
    answers: dict[str, Any] = {}
    for name, q in questions.items():
        val = raw.get(name)
        if q["type"] == "noul":
            answers[name] = NoulAnswer(noul=min(1.0, max(0.0, float(val if isinstance(val, (int, float)) else 0.5))))
        elif q["type"] == "choice":
            labels = list((q.get("criteria") or {}).keys())
            val = val if isinstance(val, dict) else {}
            ps = _normalise([val.get(k, 0) for k in labels])
            probs = dict(zip(labels, ps))
            best = max(probs, key=probs.get)
            answers[name] = ChoiceAnswer(choice=best, confidence=probs[best], probabilities=probs)
        else:
            crit = list(q.get("criteria") or [])
            vals = list(val) if isinstance(val, list) else []
            ps = _normalise((vals + [0] * len(crit))[:len(crit)])
            legend = {i: (c if isinstance(c, (str, dict, list)) else str(c)) for i, c in enumerate(crit)}
            answers[name] = ScoreAnswer(score=sum(i * p for i, p in enumerate(ps)), confidence=max(ps), legend=legend,
                                        probabilities=dict(enumerate(ps)))
    return SystemOneResponse(model="claude-fallback", usage=Usage(input_tokens=0, output_tokens=0), answers=answers)


class _Session:
    """One long-lived Claude Code process answering one message at a time."""
    MAX_TURNS = 15
    IDLE_SECONDS = 300
    ANSWER_TIMEOUT = 240

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.turns = 0
        self.model: str | None = None
        self.last_used = 0.0
        threading.Thread(target=self._reaper, daemon=True).start()
        atexit.register(self.close)

    def _start(self, model: str | None) -> None:
        from .claude_code_backend import find_claude_binary
        from .config import env
        binary = find_claude_binary()
        if not binary:
            raise RuntimeError("Claude Code binary not found; Claude cannot stand in for TypeSafe")
        child_env = dict(os.environ)
        child_env.pop("CLAUDECODE", None)
        token = env("CLAUDE_CODE_OAUTH_TOKEN")
        if token:
            child_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        cmd = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--system-prompt", SYSTEM, "--tools", "Read", "--allowedTools", "Read", "--no-session-persistence"]
        if model:
            cmd += ["--model", model]
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace", env=child_env, cwd=str(IMAGE_DIR),
                                     bufsize=1, creationflags=flags)
        lines: queue.Queue = queue.Queue()
        out = self.proc.stdout

        def pump() -> None:
            for x in out:
                lines.put(x)
            lines.put(None)
        threading.Thread(target=pump, daemon=True).start()
        self.lines, self.turns, self.model = lines, 0, model
        log.info("Claude stands in for TypeSafe: started a %s session", model or "default-model")

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()

    def _reaper(self) -> None:
        while True:
            time.sleep(30)
            if self.proc and time.time() - self.last_used > self.IDLE_SECONDS and self.lock.acquire(blocking=False):
                try:
                    if time.time() - self.last_used > self.IDLE_SECONDS:
                        self.close()
                finally:
                    self.lock.release()

    def ask(self, text: str, model: str | None) -> dict[str, Any]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None or self.turns >= self.MAX_TURNS or model != self.model:
                self.close()
                self._start(model)
            self.turns += 1
            self.last_used = time.time()
            try:
                self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
                self.proc.stdin.flush()
                deadline = time.time() + self.ANSWER_TIMEOUT
                while True:
                    line = self.lines.get(timeout=max(1.0, deadline - time.time()))
                    if line is None:
                        raise RuntimeError("the Claude session ended unexpectedly")
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "result":
                        break
            except queue.Empty:
                self.close()
                raise RuntimeError(f"Claude did not answer within {self.ANSWER_TIMEOUT}s") from None
            except Exception:
                self.close()
                raise
            finally:
                self.last_used = time.time()
            if msg.get("is_error"):
                self.close()
                raise RuntimeError(f"Claude failed: {str(msg.get('result') or msg.get('subtype'))[:300]}")
            return msg


_SESSION: _Session | None = None
_SESSION_LOCK = threading.Lock()


def _session() -> _Session:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = _Session()
        return _SESSION


def _ask_json(prompt: str, questions: dict[str, dict[str, Any]], model: str | None) -> dict[str, Any]:
    """One answer with every question's key, asked again once when the reply is not that."""
    from .claude_code_backend import _extract_json_object
    last = ""
    for attempt in (1, 2):
        text = prompt if attempt == 1 else (prompt + "\n\nREPLY WITH THE JSON OBJECT ONLY, one key per question: "
                                            + ", ".join(questions))
        msg = _session().ask(text, model)
        try:
            from .usage import record
            record(claude_calls=1)
        except Exception:  # noqa: BLE001
            pass
        last = str(msg.get("result") or "")
        raw = _extract_json_object(last)
        if raw and all(k in raw for k in questions):
            return raw
    raise RuntimeError(f"Claude's answer for TypeSafe's questions was not usable: {last[:200]!r}")


def answer(state: Any, questions: Mapping[str, Any], **_ignored: Any):
    """TypeSafe's system_one, answered by Claude."""
    qs = {str(k): _plain_question(v) for k, v in questions.items()}
    _schema(qs)                                            # an unsupported question type fails here, before Claude
    images: list[str] = []
    plain = _plain_state(state, images)
    model = str(settings().get("fallback_model") or "") or None
    t = time.time()
    raw = _ask_json(_prompt(plain, qs, images), qs, model)
    log.info("TypeSafe out of credits: Claude answered %d question%s in %.1fs (%s)", len(qs), "" if len(qs) == 1 else "s",
             time.time() - t, ", ".join(re.sub(r"\W", "", k)[:24] for k in list(qs)[:6]))
    return to_response(qs, raw)
