"""Built-in scheduler: runs inside the web console process.

Spreads N produce runs evenly across the active hours of each day, refreshes the trend analysis
(discover -> judge -> rank -> analyze) a configurable number of times a day, rotates through the
blueprints of the latest run, and relies on Telegram delivery in `produce` to post each Short.
State lives in data/schedule.json so restarts do not repeat or lose slots.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import DATA_DIR, load_config
from .storage import latest_run_dir, load_json, save_json

log = logging.getLogger("shorts.scheduler")

STATE_PATH = DATA_DIR / "schedule.json"
TICK_SECONDS = 20
DEFAULTS = {
    "enabled": False,
    "produces_per_day": 20,
    "refresh_per_day": 1,        # discover + judge + rank + analyze
    "start_hour": 6,             # active window, local time
    "end_hour": 24,
    "blueprints_to_rotate": 5,   # cycle through the top N blueprints of the latest analysed run
    "catch_up": True,            # after downtime, run the most recent missed slot once (never all of them)
}


def schedule_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((load_config().get("schedule") or {}))
    return cfg


def slots_for(day: datetime, count: int, start_hour: float, end_hour: float) -> list[datetime]:
    """`count` times evenly spread over [start_hour, end_hour) of `day`, first slot at start_hour."""
    if count <= 0:
        return []
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    span = max(0.5, end_hour - start_hour) * 3600.0
    step = span / count
    return [base + timedelta(seconds=start_hour * 3600.0 + i * step) for i in range(count)]


class Scheduler:
    def __init__(self, submit: Callable[[str, dict[str, Any]], dict[str, Any] | None],
                 job_status: Callable[[str], str | None]):
        self._submit = submit          # returns job dict or None when another job is running
        self._job_status = job_status
        self._lock = threading.Lock()
        self.state = load_json(STATE_PATH, None) or {"date": "", "done": [], "history": [], "next_blueprint": 0}
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- persistence
    def _save(self) -> None:
        self.state["history"] = self.state.get("history", [])[-60:]
        save_json(STATE_PATH, self.state)

    def _roll_day(self, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        if self.state.get("date") != today:
            self.state["date"] = today
            self.state["done"] = []

    # ---------------------------------------------------------------- planning
    def plan(self, now: datetime | None = None) -> dict[str, Any]:
        cfg = schedule_config()
        now = now or datetime.now()
        produce = slots_for(now, int(cfg["produces_per_day"]), float(cfg["start_hour"]), float(cfg["end_hour"]))
        refresh = slots_for(now, int(cfg["refresh_per_day"]), float(cfg["start_hour"]), float(cfg["end_hour"]))
        # run the refresh a little before the first produce of its slot so new blueprints are used
        refresh = [t - timedelta(minutes=15) if t > now.replace(hour=0, minute=15) else t for t in refresh]
        done = set(self.state.get("done", [])) if self.state.get("date") == now.strftime("%Y-%m-%d") else set()
        def describe(kind: str, times: list[datetime]) -> list[dict[str, Any]]:
            return [{"kind": kind, "index": i, "time": t.strftime("%H:%M"), "past": t <= now, "done": f"{kind}:{i}" in done}
                    for i, t in enumerate(times)]
        items = describe("produce", produce) + describe("refresh", refresh)
        upcoming = sorted((x for x in items if not x["past"]), key=lambda x: x["time"])
        return {"config": cfg, "today": items, "next": upcoming[:5],
                "done_today": sum(1 for x in items if x["done"] and x["kind"] == "produce"),
                "history": self.state.get("history", [])[-15:][::-1], "running": self._thread is not None}

    # ---------------------------------------------------------------- execution
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        log.info("scheduler thread started")
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            time.sleep(TICK_SECONDS)

    def _update_history_statuses(self) -> None:
        for h in self.state.get("history", []):
            if h.get("status") in ("queued", "running") and h.get("job"):
                status = self._job_status(h["job"])
                if status and status not in ("queued", "running"):
                    h["status"] = status

    def tick(self, now: datetime | None = None) -> None:
        cfg = schedule_config()
        now = now or datetime.now()
        with self._lock:
            self._roll_day(now)
            self._update_history_statuses()
            if not cfg["enabled"]:
                self._save()
                return
            done = set(self.state["done"])
            produce = slots_for(now, int(cfg["produces_per_day"]), float(cfg["start_hour"]), float(cfg["end_hour"]))
            refresh = [t - timedelta(minutes=15) for t in
                       slots_for(now, int(cfg["refresh_per_day"]), float(cfg["start_hour"]), float(cfg["end_hour"]))]
            # refresh first: fresh blueprints matter more than one produce slot
            for kind, times in (("refresh", refresh), ("produce", produce)):
                due = [(i, t) for i, t in enumerate(times) if t <= now and f"{kind}:{i}" not in done]
                if not due:
                    continue
                if not cfg["catch_up"] and len(due) > 1:
                    for i, _ in due[:-1]:
                        done.add(f"{kind}:{i}")
                    due = due[-1:]
                elif len(due) > 1:               # downtime: run the latest missed slot once, drop the rest
                    log.info("%d missed %s slots; running one catch-up", len(due) - 1, kind)
                    for i, _ in due[:-1]:
                        done.add(f"{kind}:{i}")
                    due = due[-1:]
                i, t = due[0]
                if self._fire(kind, i, t, cfg):
                    done.add(f"{kind}:{i}")
                    self.state["done"] = sorted(done)
                    self._save()
                    return                    # one job per tick; the lock allows one job anyway
            self.state["done"] = sorted(done)
            self._save()

    def _fire(self, kind: str, index: int, slot: datetime, cfg: dict[str, Any]) -> bool:
        if kind == "refresh":
            params: dict[str, Any] = {"top": 20}
            job = self._submit("run", params)
        else:
            run_dir = latest_run_dir()
            analysis = load_json(run_dir / "analysis.json") if run_dir else None
            blueprints = (analysis or {}).get("blueprints") or []
            if not blueprints:
                log.warning("scheduled produce skipped: no analysed run with blueprints yet (a refresh will run first)")
                job = self._submit("run", {"top": 20})
                if job:
                    self.state["history"].append({"kind": "refresh (no blueprints yet)", "slot": slot.strftime("%Y-%m-%d %H:%M"),
                                                  "started": datetime.now().strftime("%H:%M:%S"), "job": job["id"], "status": job["status"]})
                return job is not None
            n = max(1, min(int(cfg["blueprints_to_rotate"]), len(blueprints)))
            idx = int(self.state.get("next_blueprint", 0)) % n
            params = {"run": run_dir.name, "blueprint": idx + 1, "music": "random", "scheduled": True}
            job = self._submit("produce", params)
            if job is not None:                # advance the rotation only when the job really started
                self.state["next_blueprint"] = (idx + 1) % n
        if job is None:
            return False                       # another job is running; retry on the next tick
        self.state["history"].append({"kind": kind, "slot": slot.strftime("%Y-%m-%d %H:%M"),
                                      "started": datetime.now().strftime("%H:%M:%S"), "job": job["id"],
                                      "status": job["status"], "params": {k: v for k, v in params.items() if k != "scheduled"}})
        log.info("scheduled %s (slot %s) -> job %s", kind, slot.strftime("%H:%M"), job["id"])
        return True
