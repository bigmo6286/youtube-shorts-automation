"""External binaries the renderer needs (ffmpeg + ffprobe), with a self-install for Windows."""
from __future__ import annotations

import glob
import logging
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import requests

from .config import DATA_DIR, SHARED_DIR, env

log = logging.getLogger(__name__)

BIN_DIR = SHARED_DIR / "bin"
# gyan.dev "essentials" is a static Windows build that includes libass (needed for the caption filter).
FFMPEG_WIN_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def _has_both(directory: Path) -> bool:
    exe = ".exe" if os.name == "nt" else ""
    return (directory / f"ffmpeg{exe}").exists() and (directory / f"ffprobe{exe}").exists()


def find_ffmpeg() -> Path | None:
    """Directory containing ffmpeg and ffprobe: FFMPEG_DIR, then PATH, then our own download."""
    explicit = env("FFMPEG_DIR")
    if explicit and _has_both(Path(explicit)):
        return Path(explicit)
    on_path = shutil.which("ffmpeg")
    if on_path and shutil.which("ffprobe"):
        return Path(on_path).parent
    # Places installers put ffmpeg without the running process seeing the PATH change yet
    # (winget / chocolatey / scoop / our own download). New PATH entries only reach new processes.
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        str(BIN_DIR / "ffmpeg*" / "bin"),
        os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin"),
        os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin"),
        os.path.join(local, "Microsoft", "WinGet", "Links"),
        r"C:\ProgramData\chocolatey\bin",
        os.path.join(os.path.expanduser("~"), "scoop", "shims"),
        r"C:\ffmpeg\bin",
    ]
    for pattern in patterns:
        for candidate in sorted(glob.glob(pattern), reverse=True):
            if _has_both(Path(candidate)):
                return Path(candidate)
    return None


def ensure_ffmpeg_on_path() -> Path | None:
    found = find_ffmpeg()
    if found and str(found) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = str(found) + os.pathsep + os.environ.get("PATH", "")
    return found


MISSING_HELP = ("ffmpeg/ffprobe not found. Click 'Install ffmpeg' on the console Overview, run "
                "`python main.py setup-ffmpeg`, or install ffmpeg yourself and set FFMPEG_DIR in .env.")


def install_ffmpeg() -> Path:
    """Download a portable ffmpeg into data/bin (Windows). Other platforms use the system package manager."""
    found = find_ffmpeg()
    if found:
        log.info("ffmpeg already available at %s", found)
        return found
    if platform.system() != "Windows":
        raise RuntimeError("Automatic install is Windows-only. Install ffmpeg with your package manager "
                           "(e.g. `brew install ffmpeg` or `sudo apt install ffmpeg`) and re-run.")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = BIN_DIR / "ffmpeg-release-essentials.zip"
    log.info("downloading %s (about 90 MB)...", FFMPEG_WIN_ZIP)
    with requests.get(FFMPEG_WIN_ZIP, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        next_mark = 10
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done * 100 // total >= next_mark:
                    log.info("  %d%%", next_mark)
                    next_mark += 10
    log.info("extracting...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(BIN_DIR)
    zip_path.unlink(missing_ok=True)
    found = ensure_ffmpeg_on_path()
    if not found:
        raise RuntimeError(f"ffmpeg zip extracted under {BIN_DIR} but no bin/ffmpeg.exe was found")
    log.info("ffmpeg installed at %s", found)
    return found


CONTROL_C_EXIT = 3221225786          # 0xC000013A: Windows killed the child with a console control event


def run(cmd: list[str], *, retries: int = 1, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for ffmpeg/ffprobe/claude: never opens a console window on Windows (the autostart
    console runs under pythonw, where every child would otherwise pop up its own window that a stray click
    or Ctrl+C can kill), never waits on stdin, keeps stderr for the error message, and retries once when
    Windows reports the child was interrupted rather than failed."""
    cmd = list(cmd)
    if cmd and Path(cmd[0]).stem.lower() == "ffmpeg" and "-nostdin" not in cmd:
        cmd.insert(1, "-nostdin")
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if not kwargs.get("capture_output") and "stderr" not in kwargs:
        kwargs["stderr"] = subprocess.PIPE
    text = kwargs.get("text") or kwargs.get("encoding")
    check = kwargs.pop("check", False)
    for attempt in range(retries + 1):
        proc = subprocess.run(cmd, **kwargs)
        if proc.returncode == CONTROL_C_EXIT and attempt < retries:
            log.warning("%s was interrupted by a console control event; retrying once", Path(cmd[0]).name)
            continue
        break
    if check and proc.returncode != 0:
        err = proc.stderr if text else (proc.stderr or b"").decode("utf-8", "replace")
        tail = (err or "").strip()[-600:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed (exit {proc.returncode}){': ' + tail if tail else ''}")
    return proc
