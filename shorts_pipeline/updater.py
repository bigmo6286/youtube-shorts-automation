"""Self-update: pull the latest engine code from GitHub, install requirements, restart the console.

`check()` fetches and reports how far behind this machine is. `apply()` pulls (fast-forward only, so local edits are
never overwritten: it stops and says which files are in the way), installs requirements when they changed, and
restarts the console through a detached helper (the console cannot restart itself from inside a request). The console
has an Update button, Telegram has /update, and the scheduler can check nightly (update.auto).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import ROOT, load_config
from .tools import run

log = logging.getLogger(__name__)

DEFAULTS = {"auto": False, "hour": 4, "branch": "main"}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(load_config().get("update") or {})
    return cfg


def _git(*args: str, timeout: int = 120) -> str:
    proc = run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout, check=True, retries=0)
    return (proc.stdout or "").strip()


def check(fetch: bool = True) -> dict[str, Any]:
    """{'local': sha, 'remote': sha, 'behind': n, 'ahead': n, 'changes': [subjects], 'dirty': [files]}"""
    branch = config()["branch"]
    try:
        if fetch:
            _git("fetch", "--quiet", "origin", branch, timeout=180)
        local = _git("rev-parse", "--short", "HEAD")
        remote = _git("rev-parse", "--short", f"origin/{branch}")
        behind = int(_git("rev-list", "--count", f"HEAD..origin/{branch}") or 0)
        ahead = int(_git("rev-list", "--count", f"origin/{branch}..HEAD") or 0)
        changes = [l for l in _git("log", "--format=%s", f"HEAD..origin/{branch}").splitlines() if l] if behind else []
        dirty = [l[3:] for l in _git("status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    return {"local": local, "remote": remote, "behind": behind, "ahead": ahead, "changes": changes[:15], "dirty": dirty}


def apply(restart: bool = True) -> dict[str, Any]:
    """Pull, install requirements if they changed, then restart the console (detached). Returns what happened."""
    info = check()
    if info.get("error"):
        return {"ok": False, "message": f"git: {info['error']}"}
    if info["dirty"]:
        return {"ok": False, "message": "Local changes would be overwritten: " + ", ".join(info["dirty"][:5])
                + ". Commit or discard them (git stash) and update again."}
    if not info["behind"]:
        return {"ok": True, "updated": False, "message": f"Already up to date ({info['local']})."}
    before_req = (ROOT / "requirements.txt").read_text(encoding="utf-8", errors="replace")
    try:
        _git("pull", "--ff-only", "--quiet", "origin", config()["branch"], timeout=300)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"git pull failed: {str(exc)[:300]}"}
    after = check(fetch=False)
    pip_note = ""
    if (ROOT / "requirements.txt").read_text(encoding="utf-8", errors="replace") != before_req:
        try:
            run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")], capture_output=True, text=True,
                timeout=900, check=True, retries=0)
            pip_note = " Requirements installed."
        except Exception as exc:  # noqa: BLE001
            pip_note = f" Requirements install failed: {str(exc)[:160]}"
    message = f"Updated {info['local']} -> {after.get('local', '?')}: " + "; ".join(info["changes"][:5]) + "." + pip_note
    if restart:
        schedule_restart()
        message += " The console restarts in a few seconds."
    log.info("%s", message)
    return {"ok": True, "updated": True, "message": message, "changes": info["changes"]}


def schedule_restart(port: int | None = None, delay: int = 3) -> None:
    """A detached helper waits, stops this console and starts it again (the watchdog keeps running)."""
    from .config import CHANNEL, channels
    if port is None:
        port = 8787
        if CHANNEL:
            port = next((c["port"] for c in channels() if c["name"] == CHANNEL), port)
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    args = [str(pythonw if pythonw.exists() else exe), str(ROOT / "main.py")]
    if CHANNEL:
        args += ["--channel", CHANNEL]
    args += ["restart", "--port", str(port), "--delay", str(delay)]
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(args, cwd=str(ROOT), creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)


def restart_console(port: int, delay: int = 3) -> str:
    """Run by the helper: stop the console process and start a fresh one (and a watchdog if none runs)."""
    from . import watchdog
    time.sleep(max(0, delay))
    pid = watchdog._listening_pid(port)
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(2)
    watchdog.STOP_FLAG.unlink(missing_ok=True)
    watchdog._start_console(port)
    watchdog._start_console(port, "watchdog")          # ends at once when a watchdog already runs
    return f"console restarted on port {port}"


def maybe_nightly(now) -> str | None:
    """Scheduler hook: once a night at update.hour, when update.auto is on and nothing is running."""
    cfg = config()
    if not cfg["auto"] or now.hour != int(cfg["hour"]):
        return None
    from .config import DATA_DIR
    stamp = DATA_DIR / "update_checked.txt"
    today = now.strftime("%Y-%m-%d")
    try:
        if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == today:
            return None
        stamp.write_text(today, encoding="utf-8")
    except OSError:
        pass
    result = apply(restart=True)
    try:
        from .notify import send_message
        if result.get("updated"):
            send_message("⬆️ " + result["message"])
        elif not result.get("ok"):
            send_message("⚠️ Update failed: " + result["message"])
    except Exception:  # noqa: BLE001
        pass
    return result.get("message")


_ = os
