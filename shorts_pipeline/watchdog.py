"""Console watchdog and keep-awake.

The Startup entry runs `main.py watchdog`, which starts the console and then checks every minute that it answers.
If it does not answer three times in a row (a crash, or a hang), the watchdog stops the stuck process, starts the
console again and sends a Telegram alert. Only one watchdog runs per channel (a lock file).

`python main.py stop` (or the "Stop Shorts console" shortcut on the desktop) leaves a stop note the watchdog obeys:
it stops the console and the watchdog ends instead of restarting it. `python main.py start` brings both back, and
the next logon starts them as usual (a new watchdog clears an old note).

Keep-awake: while the schedule is on and inside its active hours, the console asks Windows not to sleep
(SetThreadExecutionState). The screen can still turn off; closing a laptop lid may still sleep it, depending on the
power settings.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from .config import CHANNEL, DATA_DIR, ROOT

log = logging.getLogger(__name__)

CHECK_EVERY = 60          # seconds between checks
FAILS_BEFORE_RESTART = 3  # three missed checks in a row
STARTUP_GRACE = 120       # seconds a freshly started console gets before it is checked
STOP_FLAG = DATA_DIR / "console.stop"


def _alive(port: int) -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/jobs", timeout=30).status_code == 200
    except requests.RequestException:
        return False


def _listening_pid(port: int) -> int | None:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            return int(parts[4])
    return None


def _start_console(port: int, command: str = "web") -> None:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    args = [str(pythonw if pythonw.exists() else exe), str(ROOT / "main.py")]
    if CHANNEL:
        args += ["--channel", CHANNEL]
    args += [command, "--port", str(port)]
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(args, cwd=str(ROOT), creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def _single_instance():
    """Keep the lock file open for the watchdog's lifetime; None when another watchdog holds it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(DATA_DIR / "watchdog.lock", "a+b")  # noqa: SIM115
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def run(port: int) -> None:
    lock = _single_instance()
    if lock is None:
        log.info("another watchdog is already running for this channel")
        return
    from .alerts import send
    STOP_FLAG.unlink(missing_ok=True)                   # a new logon or `start`: an old stop note no longer applies
    log.info("watchdog for the console on port %d", port)
    if not _alive(port) and _listening_pid(port) is None:
        _start_console(port)
        time.sleep(STARTUP_GRACE)
    fails, down_since = 0, None
    while True:
        if STOP_FLAG.exists():
            log.info("stopped on request (python main.py stop); the watchdog ends")
            return
        if _alive(port):
            if down_since:
                log.info("console answering again")
            fails, down_since = 0, None
        else:
            fails += 1
            down_since = down_since or time.time()
            if fails >= FAILS_BEFORE_RESTART:
                pid = _listening_pid(port)
                if pid:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    time.sleep(3)
                _start_console(port)
                minutes = (time.time() - down_since) / 60
                log.warning("console did not answer for %.0f min; restarted", minutes)
                send("watchdog:restart", f"🔄 The Shorts console stopped answering for {minutes:.0f} min "
                                         f"({'it was hung' if pid else 'it was not running'}); it has been restarted.",
                     repeat_hours=0.5)
                fails, down_since = 0, None
                time.sleep(STARTUP_GRACE)
                continue
        time.sleep(CHECK_EVERY)


def running_job(port: int) -> str | None:
    """The kind of job the console is running right now, if any."""
    try:
        jobs = requests.get(f"http://127.0.0.1:{port}/api/jobs", timeout=30).json()
    except Exception:  # noqa: BLE001
        return None
    return next((j["kind"] for j in jobs if j.get("status") == "running"), None)


def stop(port: int, now: bool = False) -> str:
    """Stop the console for good (until `start` or the next logon). Returns what happened."""
    job = running_job(port)
    if job and not now:
        return (f"The console is running a {job} job right now. Run this again when it is done, or add --now to "
                "stop anyway (that job is lost).")
    STOP_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    pid = _listening_pid(port)
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (f"Console stopped (it was process {pid}). The watchdog sees the stop note within a minute and ends; "
                "nothing restarts it until you run start, or until the next logon.")
    return "The console was not running. The stop note is set, so a watchdog will not start it."


def start(port: int) -> str:
    """Clear the stop note and start the watchdog (which starts the console)."""
    STOP_FLAG.unlink(missing_ok=True)
    if not _alive(port) and _listening_pid(port) is None:
        _start_console(port)
    _start_console(port, "watchdog")                    # ends at once when a watchdog is already running
    return f"Starting: the console opens on http://127.0.0.1:{port} in a few seconds, with its watchdog."


# ---------------------------------------------------------------------------------------------------- keep-awake
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
_awake = False


def keep_awake(on: bool) -> None:
    """Ask Windows not to sleep while `on` (call from a long-lived thread; it applies to that thread)."""
    global _awake
    if os.name != "nt" or on == _awake:
        return
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        _awake = on
        log.info("keep-awake %s", "on (schedule active)" if on else "off (outside the schedule window)")
    except Exception as exc:  # noqa: BLE001
        log.debug("keep-awake failed: %s", exc)
