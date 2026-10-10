"""Nightly backup of what the engine has learned, so a deleted folder or a failed disk does not erase it.

Backed up (zip): history, channel stats and analytics, labels and judgment caches, channel winners, tuning, queue,
schedule, comments, specials, cloned-style profiles, settings (config.local.yaml) and every Short's details and script.
Never backed up: the YouTube sign-in token, .env (API keys) and client_secrets.json.

Destination: `backup.folder` if set, else OneDrive\\ShortsBackups (synced off this PC by OneDrive) when OneDrive is set
up, else ShortsBackups in the user folder. The newest `backup.keep` zips are kept. Restore with
`python main.py backup restore <zip>` (overwrites the learning files, never tokens or keys).
"""
from __future__ import annotations

import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any

from .config import CHANNEL, DATA_DIR, HOME, LOCAL_CONFIG, OUTPUT_DIR, SHARED_DIR, load_config

log = logging.getLogger(__name__)

NEVER = {"youtube_token.json", ".env", "client_secrets.json"}
DATA_FILES = ["produced_titles.json", "channel_stats.json", "channel_analytics.json", "channel_blueprints.json",
              "tuning.json", "upload_queue.json", "schedule.json", "comments.json", "specials.json", "api_discovery.json",
              "alerts.json"]
SHARED_FILES = ["cache/channel_judgments.json", "cache/judgments.json", "channels.json"]
DEFAULTS = {"enabled": True, "folder": "", "keep": 14, "hour": 3}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(load_config().get("backup") or {})
    return cfg


def folder() -> Path:
    cfg = config()
    if cfg["folder"]:
        base = Path(os.path.expandvars(cfg["folder"]))
    elif os.environ.get("OneDrive") and Path(os.environ["OneDrive"]).exists():
        base = Path(os.environ["OneDrive"]) / "ShortsBackups"
    else:
        base = Path.home() / "ShortsBackups"
    return base / (CHANNEL or "main")


def _files() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for name in DATA_FILES:
        p = DATA_DIR / name
        if p.exists():
            out.append((p, f"data/{name}"))
    for name in SHARED_FILES:
        p = SHARED_DIR / name
        if p.exists():
            out.append((p, f"shared/{name}"))
    profiles = DATA_DIR / "profiles"
    if profiles.exists():
        out += [(p, f"data/profiles/{p.name}") for p in profiles.glob("*.json")]
    if LOCAL_CONFIG.exists():
        out.append((LOCAL_CONFIG, "config.local.yaml"))
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            for name in ("meta.json", "script.json"):
                p = d / name
                if p.exists():
                    out.append((p, f"output/{d.name}/{name}"))
    return [(p, arc) for p, arc in out if p.name not in NEVER]


def make() -> Path:
    dest = folder()
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"shorts-{CHANNEL or 'main'}-{time.strftime('%Y-%m-%d_%H%M')}.zip"
    tmp = path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p, arc in _files():
            try:
                z.write(p, arc)
            except OSError as exc:
                log.warning("backup: skipped %s (%s)", p, exc)
    tmp.replace(path)
    old = sorted(dest.glob("shorts-*.zip"))[:-int(config()["keep"])]
    for p in old:
        p.unlink(missing_ok=True)
    log.info("backup: %s (%.1f MB, %d kept)", path, path.stat().st_size / 1e6, min(len(list(dest.glob('shorts-*.zip'))), int(config()["keep"])))
    return path


def restore(zip_path: Path) -> int:
    """Put the learning files back; tokens and keys are never in a backup, so they are never overwritten."""
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for arc in z.namelist():
            name = Path(arc).name
            if name in NEVER or ".." in Path(arc).parts:
                continue
            if arc.startswith("data/"):
                target = DATA_DIR / arc[len("data/"):]
            elif arc.startswith("shared/"):
                target = SHARED_DIR / arc[len("shared/"):]
            elif arc.startswith("output/"):
                target = OUTPUT_DIR / arc[len("output/"):]
            elif arc == "config.local.yaml":
                target = LOCAL_CONFIG
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(arc))
            n += 1
    return n


def maybe_nightly(now=None) -> Path | None:
    """Once a day at `backup.hour` (called from the scheduler tick)."""
    import datetime as _dt
    cfg = config()
    now = now or _dt.datetime.now()
    if not cfg["enabled"] or now.hour < int(cfg["hour"]):
        return None
    stamp = DATA_DIR / "last_backup.txt"
    today = now.strftime("%Y-%m-%d")
    if stamp.exists() and stamp.read_text().strip() == today:
        return None
    try:
        path = make()
    except Exception as exc:  # noqa: BLE001
        log.warning("nightly backup failed: %s", exc)
        from .alerts import send
        send("backup", f"⚠️ Nightly backup failed: {str(exc)[:200]}")
        return None
    stamp.write_text(today)
    return path
