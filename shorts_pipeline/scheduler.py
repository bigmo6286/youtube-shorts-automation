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
REUSE_RUN_HOURS = 3          # a trend refresh is skipped when a run (any channel's) is younger than this
RETRY_DELAY_MINUTES = 30     # a scheduled production that failed for a temporary reason runs again this much later
EXPIRE_EVERY_SECONDS = 600
CLEANUP_EVERY_SECONDS = 6 * 3600
COMMENTS_EVERY_SECONDS = 2 * 3600
DEFAULTS = {
    "enabled": False,
    "produces_per_day": 20,
    "refresh_per_day": 2,        # discover + judge + rank + analyze, so the ranking reflects what is trending now
    "start_hour": 6,             # active window, local time
    "end_hour": 24,
    "blueprints_to_rotate": 12,  # consider at most the top N trend blueprints (plus your channel's own winners)
    "selection": "weighted",     # weighted = pick in proportion to opportunity score | rotate = round robin
    "min_share": 0.2,            # a blueprint must score at least this fraction of the best one to be produced
    "skip_stretch": True,        # never auto-produce formats that need a person on camera
    "channel_feedback": True,    # scale blueprint weights by how each format x topic performs on your channel
    "source": "trends",          # trends = blueprints from the trend ranking | profile = clone a channel's style
    "profile": "",               # handle of an analysed channel profile (see `profile add`) when source = profile
    "catch_up": True,            # after downtime, run the most recent missed slot once (never all of them)
    "retry_failed": True,        # re-run a production that failed for a temporary reason once, later the same day
    "skip_when_queue_full": True,  # skip a production slot while the upload queue already holds a day's uploads
}


def schedule_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((load_config().get("schedule") or {}))
    return cfg


def eligible_blueprints(blueprints: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """The blueprints worth producing: top N, not stretch, and within `min_share` of the best score.
    Each entry gets `index` (1-based, as `produce --blueprint` expects) and `weight`."""
    top_n = max(1, int(cfg.get("blueprints_to_rotate", 5)))
    pool = [dict(b, index=i + 1) for i, b in enumerate(blueprints[:top_n])]
    if cfg.get("skip_stretch", True):
        pool = [b for b in pool if not b.get("stretch")] or pool[:1]
    # Channel feedback: the trend score says what the world watches, your channel says what YOUR viewers
    # watch. The trend score is normalised and square-rooted so it cannot dominate; the channel factor
    # (0.2 .. 4, from views/hour vs your channel median) multiplies it and therefore decides the mix once
    # you have uploads. Formats and topics seen on your channel inform pairs never tried yet.
    perf: dict[str, Any] = {}
    if cfg.get("channel_feedback", True):
        try:
            from .channel import blueprint_performance
            perf = blueprint_performance()
        except Exception as exc:  # noqa: BLE001
            log.debug("channel feedback unavailable: %s", exc)
    top_trend = max((float(b.get("opportunity") or 0) for b in pool), default=0.0) or 1.0
    for b in pool:
        factor, videos, basis = (1.0, 0, "none")
        if perf:
            from .channel import factor_for
            factor, videos, basis = factor_for(perf, b.get("format", ""), b.get("topic", ""))
        b["channel_factor"], b["channel_videos"], b["channel_basis"] = factor, videos, basis
        b["source"] = "trends"
        trend_norm = (float(b.get("opportunity") or 0) / top_trend) ** 0.5
        b["adjusted"] = trend_norm * factor
    # Your channel's own winners join the pool even when the trend run has no such blueprint: a format x topic
    # that beats your channel median by 30%+ is worth more videos regardless of what is trending globally.
    if perf and cfg.get("channel_feedback", True):
        try:
            from .channel import channel_blueprints
            from .judge import canonical_topic
            have = {f"{b.get('format')}|{canonical_topic(b.get('topic', ''))}" for b in pool}
            for cb in channel_blueprints(perf):
                if cb["key"] in have:
                    continue
                cb = dict(cb, index=f"channel:{cb['key']}", channel_factor=float(cb["opportunity"]),
                          channel_videos=int(cb["count"]), channel_basis="pair")
                cb["adjusted"] = 0.6 * cb["channel_factor"]        # as a mid-ranked trend blueprint would score
                pool.append(cb)
        except Exception as exc:  # noqa: BLE001
            log.debug("channel blueprints unavailable: %s", exc)
    best = max((b["adjusted"] for b in pool), default=0.0)
    floor = best * float(cfg.get("min_share", 0.25))
    pool = [b for b in pool if b["adjusted"] >= floor] or pool[:1]
    for b in pool:
        b["weight"] = round(b["adjusted"] / best, 3) if best else 1.0
    return pool


def choose_blueprint(blueprints: list[dict[str, Any]], cfg: dict[str, Any], state: dict[str, Any]) -> int | str | None:
    """1-based trend blueprint index (or 'channel:<format>|<topic>') for the next produce, or None."""
    import random

    pool = eligible_blueprints(blueprints, cfg)
    if not pool:
        return None
    if cfg.get("selection", "weighted") == "rotate" or len(pool) == 1:
        idx = int(state.get("next_blueprint", 0)) % len(pool)
        state["next_blueprint"] = (idx + 1) % len(pool)
        return pool[idx]["index"]
    # weighted by opportunity, with the blueprint produced last time slightly discouraged so a strong
    # leader still does not turn the whole day into one format
    last = state.get("last_blueprint_key")
    weights = [b["weight"] * (0.5 if f"{b['format']}|{b['topic']}" == last and len(pool) > 1 else 1.0) for b in pool]
    pick = random.choices(pool, weights=weights, k=1)[0]
    state["last_blueprint_key"] = f"{pick['format']}|{pick['topic']}"
    return pick["index"]


def explore_choice(blueprints: list[dict[str, Any]], cfg: dict[str, Any]) -> int | None:
    """About `explore_share` of productions try a format x topic pair the channel has never made, picked from the trend
    run by opportunity, so new winners can be found instead of only repeating known ones. Returns a 1-based index."""
    import random
    try:
        from .specials import config as specials_config
        share = float(specials_config()["explore_share"])
    except Exception:  # noqa: BLE001
        share = 0.0
    if share <= 0 or random.random() >= share:
        return None
    tried: set[str] = set()
    try:
        from .channel import labelled_uploads
        from .history import known_videos
        from .judge import canonical_topic
        tried |= {f"{u.get('format')}|{canonical_topic(u.get('topic') or '')}" for u in labelled_uploads()}
        tried |= {k.get("key", "") for k in known_videos()}
    except Exception:  # noqa: BLE001
        return None
    untried = [(i + 1, b) for i, b in enumerate(blueprints)
               if not (cfg.get("skip_stretch", True) and b.get("stretch")) and f"{b.get('format')}|{b.get('topic')}" not in tried]
    if not untried:
        return None
    index, bp = random.choices(untried, weights=[max(0.01, float(b.get("opportunity") or 0)) for _, b in untried], k=1)[0]
    log.info("exploration: trying %s x %s, never made on this channel", bp.get("format"), bp.get("topic"))
    return index


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
                 job_status: Callable[[str], str | None],
                 job_error: Callable[[str], str | None] | None = None):
        self._submit = submit          # returns job dict or None when another job is running
        self._job_status = job_status
        self._job_error = job_error or (lambda _id: None)
        self._last_expire = 0.0
        self._last_cleanup = 0.0
        self._last_comments = 0.0
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
            self.state["retries"] = []          # retries belong to the day of the failed slot

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
        run_dir = latest_run_dir()
        analysis = load_json(run_dir / "analysis.json") if run_dir else None
        pool = eligible_blueprints((analysis or {}).get("blueprints") or [], cfg)
        return {"config": cfg, "today": items, "next": upcoming[:5],
                "done_today": sum(1 for x in items if x["done"] and x["kind"] == "produce"),
                "history": self.state.get("history", [])[-15:][::-1], "running": self._thread is not None,
                "run": run_dir.name if run_dir else None,
                "eligible": [{k: b.get(k) for k in ("index", "format", "topic", "hook_style", "opportunity", "weight", "count",
                                                      "channel_factor", "channel_videos", "channel_basis", "source")} for b in pool],
                "source": cfg.get("source", "trends"), "profile": cfg.get("profile", ""),
                "retries": self.state.get("retries", []), "queue": _queue_summary(), "publish": _publish_summary(),
                "tuning": _tuning_summary()}

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
        try:
            from .watchdog import keep_awake
            keep_awake(bool(cfg["enabled"]) and float(cfg["start_hour"]) <= now.hour + now.minute / 60 < float(cfg["end_hour"]))
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._roll_day(now)
            self._update_history_statuses()
            try:
                from .backup import maybe_nightly
                maybe_nightly(now)
            except Exception:  # noqa: BLE001
                log.exception("nightly backup failed")
            if time.time() - self._last_cleanup > CLEANUP_EVERY_SECONDS:
                self._last_cleanup = time.time()
                try:
                    from .housekeeping import cleanup_outputs
                    cleanup_outputs()
                except Exception:  # noqa: BLE001
                    log.exception("output cleanup failed")
            if not cfg["enabled"]:
                self._save()
                return
            self._schedule_retries(cfg, now)
            try:
                from .alerts import maybe_send_daily_report
                maybe_send_daily_report(now)
            except Exception:  # noqa: BLE001
                log.exception("daily report failed")
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
                return                        # a slot is due but a job is running: do not start anything else
            self.state["done"] = sorted(done)
            # nothing scheduled is due: a pending retry first, then the upload queue
            due_retries = [r for r in self.state.get("retries", []) if datetime.fromisoformat(r["due"]) <= now]
            if due_retries:
                r = due_retries[0]
                if self._fire("produce", -1, datetime.fromisoformat(r["due"]), cfg, retry_of=r.get("slot")):
                    self.state["retries"].remove(r)
                self._save()
                return
            self._upload_from_queue(now)
            self._run_comments(now)
            self._save()

    # ---------------------------------------------------------------- retries and the upload queue
    def _schedule_retries(self, cfg: dict[str, Any], now: datetime) -> None:
        """A scheduled production that failed for a temporary reason (network, a crashed render, the voice
        service, every draft repeating a subject) gets one more run RETRY_DELAY_MINUTES later, if that is still
        inside today's window. A retry that fails is not retried again."""
        if not cfg.get("retry_failed", True):
            return
        from .alerts import classify
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=float(cfg["end_hour"]))
        for h in self.state.get("history", []):
            if h.get("kind") != "produce" or h.get("status") != "error" or h.get("retry_checked"):
                continue
            h["retry_checked"] = True
            problem = classify(self._job_error(h.get("job", "")))
            if not problem.retryable:
                continue
            due = now + timedelta(minutes=RETRY_DELAY_MINUTES)
            if due >= end:
                log.info("failed slot %s (%s) not retried: the day's window ends first", h.get("slot"), problem.title)
                continue
            self.state.setdefault("retries", []).append({"due": due.isoformat(timespec="seconds"), "slot": h.get("slot"),
                                                         "reason": problem.title})
            h["retry_at"] = due.strftime("%H:%M")
            log.info("slot %s failed (%s); retrying at %s", h.get("slot"), problem.title, due.strftime("%H:%M"))

    def _upload_from_queue(self, now: datetime) -> None:
        try:
            from . import upload_queue
            if time.time() - self._last_expire > EXPIRE_EVERY_SECONDS:
                self._last_expire = time.time()
                upload_queue.expire_old()
            item, _reason = upload_queue.next_upload()
            if not item:
                return
            job = self._submit("upload", {"path": f"output/{item['dir']}", "auto": True})
            if job is None:
                return
            upload_queue.mark_started()
            self.state["history"].append({"kind": "upload (auto)", "slot": now.strftime("%Y-%m-%d %H:%M"),
                                          "started": datetime.now().strftime("%H:%M:%S"), "job": job["id"],
                                          "status": job["status"],
                                          "params": {"title": item["title"][:60], "priority": item["priority"]}})
            log.info("upload queue: uploading %r (priority %d) -> job %s", item["title"][:60], item["priority"], job["id"])
        except Exception:  # noqa: BLE001
            log.exception("upload queue step failed")

    def _run_comments(self, now: datetime) -> None:
        if time.time() - self._last_comments < COMMENTS_EVERY_SECONDS:
            return
        try:
            from . import comments
            if not comments.config()["enabled"] or not comments.connected():
                return
            job = self._submit("comments", {"action": "run", "auto": True})
            if job is not None:
                self._last_comments = time.time()
                self.state["history"].append({"kind": "comments", "slot": now.strftime("%Y-%m-%d %H:%M"),
                                              "started": datetime.now().strftime("%H:%M:%S"), "job": job["id"],
                                              "status": job["status"]})
        except Exception:  # noqa: BLE001
            log.exception("comments step failed")

    def _queue_full(self) -> bool:
        try:
            from . import upload_queue
            qcfg = upload_queue.config()
            if not qcfg["auto"]:
                return False
            waiting = [i for i in upload_queue.queued() if i["priority"] >= int(qcfg["min_priority"])]
            return len(waiting) >= max(1, upload_queue.limit())
        except Exception:  # noqa: BLE001
            return False

    def _fire(self, kind: str, index: int, slot: datetime, cfg: dict[str, Any], retry_of: str | None = None) -> bool:
        if kind == "produce" and cfg.get("skip_when_queue_full", True) and self._queue_full():
            log.info("produce slot %s skipped: the upload queue already holds a day's worth of Shorts", slot.strftime("%H:%M"))
            self.state["history"].append({"kind": "produce (skipped: upload queue full)", "slot": slot.strftime("%Y-%m-%d %H:%M"),
                                          "started": datetime.now().strftime("%H:%M:%S"), "status": "skipped"})
            return True
        if kind == "refresh" and _recent_run_hours() is not None and _recent_run_hours() < REUSE_RUN_HOURS:
            # trend runs are shared by every channel on this machine: one made recently is reused
            log.info("refresh slot %s: a trend run from %.1f h ago is reused", slot.strftime("%H:%M"), _recent_run_hours())
            self.state["history"].append({"kind": "refresh (reused recent run)", "slot": slot.strftime("%Y-%m-%d %H:%M"),
                                          "started": datetime.now().strftime("%H:%M:%S"), "status": "skipped"})
            return True
        if kind == "refresh":
            params: dict[str, Any] = {"top": 20, "scheduled": True}
            job = self._submit("run", params)
        elif cfg.get("source") == "dub":
            from .dub import next_source
            src = next_source()
            if not src:
                log.info("dub slot %s skipped: nothing new to dub on the source channel", slot.strftime("%H:%M"))
                self.state["history"].append({"kind": "produce (skipped: nothing to dub)", "slot": slot.strftime("%Y-%m-%d %H:%M"),
                                              "started": datetime.now().strftime("%H:%M:%S"), "status": "skipped"})
                return True
            params = {"dub_of": src, "music": "random", "scheduled": True}
            job = self._submit("produce", params)
        elif cfg.get("source") == "profile" and cfg.get("profile"):
            params = {"profile": cfg["profile"], "music": "random", "scheduled": True}
            job = self._submit("produce", params)
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
            special = None
            try:
                from .specials import next_special
                special = next_special()
            except Exception:  # noqa: BLE001
                log.exception("special production check failed")
            if special:
                params = {**special, "music": "random", "scheduled": True}
                job = self._submit("produce", params)
                if job is None:
                    return False
                self.state["history"].append({"kind": "produce (sequel)" if "sequel_of" in special else "produce (viewer idea)",
                                              "slot": slot.strftime("%Y-%m-%d %H:%M"), "started": datetime.now().strftime("%H:%M:%S"),
                                              "job": job["id"], "status": job["status"], "params": special})
                log.info("scheduled special production (slot %s) -> job %s", slot.strftime("%H:%M"), job["id"])
                return True
            explore_index = explore_choice(blueprints, cfg)
            snapshot = dict(self.state)        # choose_blueprint mutates rotation state; keep it only if the job starts
            choice = explore_index or choose_blueprint(blueprints, cfg, snapshot)
            if choice is None:
                log.warning("scheduled produce skipped: no eligible blueprint")
                return False
            if isinstance(choice, str) and choice.startswith("channel:"):
                params = {"blueprint_key": choice.split(":", 1)[1], "music": "random", "scheduled": True}
            else:
                params = {"run": run_dir.name, "blueprint": choice, "music": "random", "scheduled": True}
            if explore_index:
                params["explore"] = True
            job = self._submit("produce", params)
            if job is not None:
                self.state.update({k: snapshot[k] for k in ("next_blueprint", "last_blueprint_key") if k in snapshot})
        if job is None:
            return False                       # another job is running; retry on the next tick
        entry = {"kind": kind if not retry_of else f"{kind} (retry)", "slot": slot.strftime("%Y-%m-%d %H:%M"),
                 "started": datetime.now().strftime("%H:%M:%S"), "job": job["id"],
                 "status": job["status"], "params": {k: v for k, v in params.items() if k != "scheduled"}}
        if retry_of:
            entry["retry_of"] = retry_of
        self.state["history"].append(entry)
        log.info("scheduled %s (slot %s) -> job %s", kind, slot.strftime("%H:%M"), job["id"])
        return True


def _queue_summary() -> dict[str, Any] | None:
    try:
        from .upload_queue import summary
        return summary()
    except Exception:  # noqa: BLE001
        return None


def _publish_summary() -> dict[str, Any] | None:
    try:
        from .publish_times import describe
        return describe()
    except Exception:  # noqa: BLE001
        return None


def _recent_run_hours() -> float | None:
    """Age of the newest complete trend run (with an analysis), in hours."""
    run = latest_run_dir()
    if not run or not (run / "analysis.json").exists():
        return None
    return (time.time() - (run / "analysis.json").stat().st_mtime) / 3600


def _tuning_summary() -> dict[str, Any] | None:
    try:
        from . import originality, tuning
        t = load_json(tuning.TUNING_PATH) or {}
        return {"uploads_per_day": (t.get("volume") or {}).get("recommended"), "reason": (t.get("volume") or {}).get("reason"),
                "seconds": (t.get("length") or {}).get("channel"), "formats": (t.get("length") or {}).get("formats"),
                "sameness": round(originality.sameness(), 2), "overused": [n for n, _ in originality.overused_formulas()],
                "retitles": __import__("shorts_pipeline.retitle", fromlist=["tally"]).tally()}
    except Exception:  # noqa: BLE001
        return None
