"""FastAPI backend for the console. Binds to localhost only: it can read and write your API keys."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import threading
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import cli
from ..config import OUTPUT_DIR, ROOT, load_config, save_local_config
from ..storage import RUNS_DIR, load_json

log = logging.getLogger("shorts.web")
STATIC = Path(__file__).parent / "static"
ENV_PATH = ROOT / ".env"

KEY_FIELDS = {
    "TYPESAFE_API_KEY": "TypeSafe API key (judging, ranking, script QA)",
    "ANTHROPIC_API_KEY": "Anthropic API key (script writing, optional if using a subscription token)",
    "CLAUDE_CODE_OAUTH_TOKEN": "Claude subscription token from `claude setup-token`",
    "PEXELS_API_KEY": "Pexels API key (stock footage backgrounds, optional)",
    "YOUTUBE_API_KEY": "YouTube Data API key (channel performance feedback; also an extra discovery source)",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token from @BotFather (delivery of finished Shorts)",
    "TOGETHER_API_KEY": "Together AI key (optional: FLUX images instead of the free generator)",
    "HF_TOKEN": "Hugging Face token (optional: FLUX / Stable Diffusion images via the Inference API)",
    "OPENAI_API_KEY": "OpenAI key (optional: gpt-image-1 images instead of the free generator)",
}
PATH_FIELDS = {
    "YOUTUBE_CHANNEL": "Your YouTube channel handle (e.g. @yourname) or channel id, for performance feedback",
    "TELEGRAM_CHAT_ID": "Telegram chat id to send finished Shorts to (use Find my chat ID below)",
    "FFMPEG_DIR": "Folder containing ffmpeg.exe and ffprobe.exe (optional, auto-detected or auto-installed)",
    "CLAUDE_CODE_BIN": "Path to claude.exe (optional, auto-detected)",
    "YOUTUBE_CLIENT_SECRETS": "OAuth client secrets file for uploads",
}

app = FastAPI(title="Shorts console")


# ------------------------------------------------------------------------------------ .env handling

def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def _write_env(updates: dict[str, str]) -> None:
    """Update or append keys while keeping comments and order of the existing file."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in updates:
                lines[i] = f"{k}={updates[k]}"
                seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for k, v in updates.items():           # the running server must see them too
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def _mask(value: str) -> str:
    return f"…{value[-4:]}" if len(value) >= 8 else "set"


# ------------------------------------------------------------------------------------ jobs

class Job:
    def __init__(self, kind: str, params: dict[str, Any]):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.params = params
        self.status = "queued"
        self.log: list[str] = []
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.log_file: str | None = None

    def to_dict(self, tail: int | None = None) -> dict[str, Any]:
        lines = self.log[-tail:] if tail else self.log
        return {"id": self.id, "kind": self.kind, "params": self.params, "status": self.status,
                "log": lines, "log_length": len(self.log), "result": self.result, "error": self.error,
                "started": self.started, "finished": self.finished, "log_file": self.log_file}


JOBS: dict[str, Job] = {}
_JOB_LOCK = threading.Lock()          # one pipeline job at a time: YouTube rate limits and a small CPU
_CURRENT: Job | None = None


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Job):
        super().__init__(logging.INFO)
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("typesafe_sdk") and record.levelno < logging.WARNING:
            return                       # one line per request is noise here
        if record.name in ("asyncio", "uvicorn.error", "uvicorn.access", "httpx", "httpx2", "googleapiclient.discovery_cache"):
            return                       # the web server's own chatter (browser connections closing) is not job output
        self.job.log.append(self.format(record))
        if record.exc_info:
            self.job.log.extend(traceback.format_exception(*record.exc_info)[-6:])


class _JobStdout(io.TextIOBase):
    def __init__(self, job: Job):
        self.job = job
        self._buf = ""

    def write(self, s: str) -> int:      # type: ignore[override]
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.job.log.append(line)
        return len(s)


def _args(job: Job) -> SimpleNamespace:
    p = job.params
    return SimpleNamespace(
        run=p.get("run") or None, force=bool(p.get("force")), top=int(p.get("top", 20)),
        api=bool(p.get("api")), produce=bool(p.get("produce")),
        blueprint=int(p["blueprint"]) if str(p.get("blueprint") or "").isdigit() else 1,
        blueprint_key=(p.get("blueprint_key") or (str(p.get("blueprint"))[8:] if str(p.get("blueprint") or "").startswith("channel:") else None)),
        angle=p.get("angle") or None, upload=bool(p.get("upload")), path=p.get("path"), verbose=False,
        script_text=p.get("script_text") or None, script_file=None, title=p.get("title") or "",
        description=p.get("description") or "", hashtags=p.get("hashtags") or "", keywords=p.get("keywords") or "",
        music=p.get("music") or None, action=p.get("action") or "list", query=p.get("query") or "lofi chill",
        count=int(p.get("count") or 5), intro=p.get("intro"), outro=p.get("outro"),
        scheduled=bool(p.get("scheduled")), enhance=p.get("enhance", True) is not False,
        telegram=p.get("telegram", True) is not False, privacy=p.get("privacy") or None,
        regenerate=p.get("regenerate", True) is not False, set=p.get("set", True) is not False,
        profile=p.get("profile") or None, seconds=p.get("seconds") or None, target=p.get("target") or None,
        videos=int(p.get("videos") or 24),
    )


COMMANDS = {
    "run": cli.cmd_run, "discover": cli.cmd_discover, "judge": cli.cmd_judge, "rank": cli.cmd_rank,
    "analyze": cli.cmd_analyze, "produce": cli.cmd_produce, "upload": cli.cmd_upload,
    "setup_ffmpeg": cli.cmd_setup_ffmpeg, "fetch_music": cli.cmd_music, "telegram": cli.cmd_telegram,
    "channel": cli.cmd_channel, "publish": cli.cmd_publish, "thumbnail": cli.cmd_thumbnail, "profile": cli.cmd_profile,
}


def _run_job(job: Job) -> None:
    global _CURRENT
    handler = _JobLogHandler(job)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    job.status = "running"
    _CURRENT = job
    try:
        with contextlib.redirect_stdout(_JobStdout(job)):
            result = COMMANDS[job.kind](_args(job))
        job.result = str(result) if result is not None else None
        job.status = "done"
    except SystemExit as exc:            # cli functions use sys.exit for user-facing failures
        job.error = str(exc)
        job.status = "error"
        job.log.append(f"ERROR: {exc}")
    except Exception as exc:  # noqa: BLE001
        job.error = f"{type(exc).__name__}: {exc}"
        job.status = "error"
        job.log.append(f"ERROR: {job.error}")
        job.log.extend(traceback.format_exc().rstrip().splitlines())
    finally:
        job.finished = time.time()
        root.removeHandler(handler)
        _CURRENT = None
        _JOB_LOCK.release()
        _persist_job(job)


LOG_DIR = ROOT / "data" / "logs"


def _persist_job(job: Job) -> None:
    """Keep every job log on disk so errors can be read after the page is closed."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(job.started))
        path = LOG_DIR / f"{stamp}_{job.kind}_{job.id}.log"
        header = [f"job {job.id} kind={job.kind} status={job.status} params={json.dumps(job.params)}",
                  f"error: {job.error}" if job.error else "", ""]
        path.write_text("\n".join(header + job.log) + "\n", encoding="utf-8")
        job.log_file = str(path)
    except Exception:  # noqa: BLE001
        log.exception("could not persist job log")


class JobRequest(BaseModel):
    kind: str
    params: dict[str, Any] = {}


def submit_job(kind: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Start a job unless one is running (returns None in that case)."""
    if kind not in COMMANDS:
        raise ValueError(f"unknown job kind {kind}")
    if not _JOB_LOCK.acquire(blocking=False):
        return None
    job = Job(kind, params)
    JOBS[job.id] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job.to_dict()


@app.post("/api/jobs")
def start_job(req: JobRequest) -> dict[str, Any]:
    try:
        job = submit_job(req.kind, req.params)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if job is None:
        raise HTTPException(409, f"a job is already running ({_CURRENT.kind if _CURRENT else '?'})")
    return job


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [j.to_dict(tail=1) for j in sorted(JOBS.values(), key=lambda j: -j.started)[:20]]


@app.get("/api/logs")
def list_logs() -> list[dict[str, Any]]:
    """Job logs from earlier console sessions, newest first."""
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("*.log"), reverse=True)[:50]
    return [{"name": f.name, "size": f.stat().st_size} for f in files]


@app.get("/api/logs/{name}")
def read_log(name: str) -> dict[str, Any]:
    path = LOG_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404, "no such log")
    return {"name": path.name, "text": path.read_text(encoding="utf-8", errors="replace")}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, offset: int = 0) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    d = job.to_dict()
    d["log"] = job.log[offset:]
    return d


# ------------------------------------------------------------------------------------ settings

@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    values = _read_env()
    keys = [{"name": k, "label": label, "set": bool(values.get(k)), "hint": _mask(values[k]) if values.get(k) else ""}
            for k, label in KEY_FIELDS.items()]
    paths = [{"name": k, "label": label, "value": values.get(k, "")} for k, label in PATH_FIELDS.items()]
    secrets_path = values.get("YOUTUBE_CLIENT_SECRETS") or "client_secrets.json"
    return {"keys": keys, "paths": paths, "config": load_config(),
            "client_secrets_present": (ROOT / secrets_path).exists(),
            "youtube_token_present": (ROOT / "data" / "youtube_token.json").exists()}


class SettingsUpdate(BaseModel):
    keys: dict[str, str] = {}
    paths: dict[str, str] = {}
    config: dict[str, Any] | None = None


@app.post("/api/settings")
def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    updates: dict[str, str] = {}
    for k, v in body.keys.items():
        if k in KEY_FIELDS and v is not None and v.strip() != "":
            updates[k] = v.strip()
    for k, v in body.paths.items():
        if k in PATH_FIELDS and v is not None:
            updates[k] = v.strip()
    if updates:
        _write_env(updates)
    if body.config is not None:
        save_local_config({k: v for k, v in body.config.items() if isinstance(v, dict)})
    return get_settings()


@app.delete("/api/settings/keys/{name}")
def clear_key(name: str) -> dict[str, Any]:
    if name not in KEY_FIELDS:
        raise HTTPException(400, "unknown key")
    _write_env({name: ""})
    return get_settings()


@app.post("/api/settings/client-secrets")
async def upload_client_secrets(file: UploadFile) -> dict[str, Any]:
    data = await file.read()
    if b'"installed"' not in data and b'"web"' not in data:
        raise HTTPException(400, "that does not look like a Google OAuth client JSON file")
    (ROOT / "client_secrets.json").write_bytes(data)
    _write_env({"YOUTUBE_CLIENT_SECRETS": "client_secrets.json"})
    return get_settings()


# ------------------------------------------------------------------------------------ captions

@app.get("/api/captions/presets")
def caption_presets() -> dict[str, Any]:
    from ..captions import POSITIONS, PRESETS, resolve_style
    return {"presets": {k: {"label": v["label"], **{kk: vv for kk, vv in v.items() if kk != "label"}} for k, v in PRESETS.items()},
            "positions": list(POSITIONS), "current": resolve_style((load_config().get("production") or {}).get("captions"))}


class CaptionStyleBody(BaseModel):
    style: dict[str, Any] = {}


@app.post("/api/captions/preview")
def caption_preview(body: CaptionStyleBody):
    """Render the caption style over a sample frame; returns a PNG."""
    import subprocess
    import tempfile
    from fastapi.responses import Response
    from ..captions import subtitles_filter, write_preview_ass
    from ..tools import ensure_ffmpeg_on_path
    if not ensure_ffmpeg_on_path():
        raise HTTPException(400, "ffmpeg is not available")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        ass = write_preview_ass(work / "preview.ass", body.style)
        out = work / "preview.png"
        filt = ("gradients=size=1080x1920:speed=0.01:nb_colors=3:c0=0x1b1f3b:c1=0x3a0f5c:c2=0x0b3b5c:duration=1:rate=1,"
                "format=yuv420p," + subtitles_filter(ass) + ",scale=405:720")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", filt, "-frames:v", "1", "-update", "1", out.name]
        r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise HTTPException(500, f"preview failed: {r.stderr[-300:]}")
        return Response(out.read_bytes(), media_type="image/png")


# ------------------------------------------------------------------------------------ scheduler

from ..scheduler import Scheduler  # noqa: E402

SCHEDULER = Scheduler(submit_job, lambda job_id: JOBS[job_id].status if job_id in JOBS else None)


@app.on_event("startup")
def _start_scheduler() -> None:
    SCHEDULER.start()


@app.get("/api/schedule")
def get_schedule() -> dict[str, Any]:
    return SCHEDULER.plan()


# ------------------------------------------------------------------------------------ style profiles

@app.get("/api/profiles")
def get_profiles() -> list[dict[str, Any]]:
    from ..profile import list_profiles
    return list_profiles()


@app.get("/api/profiles/{handle}")
def get_profile(handle: str) -> dict[str, Any]:
    from ..profile import load_profile
    p = load_profile(handle)
    if not p:
        raise HTTPException(404, "no such profile")
    return p


# ------------------------------------------------------------------------------------ channel feedback

@app.get("/api/channel")
def channel_report() -> dict[str, Any]:
    from ..channel import cached_report, configured
    report = cached_report()
    return {"configured": configured(), "report": report}


# ------------------------------------------------------------------------------------ telegram

@app.get("/api/telegram/discover")
def telegram_discover() -> list[dict[str, Any]]:
    from ..notify import discover_chats
    try:
        return discover_chats()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


# ------------------------------------------------------------------------------------ music

@app.get("/api/music")
def get_music() -> list[dict[str, Any]]:
    from ..music import attribution_line, list_tracks
    tracks = list_tracks()
    for t in tracks:
        t["credit"] = attribution_line(t)
    return tracks


@app.post("/api/music/upload")
async def upload_music(file: UploadFile) -> list[dict[str, Any]]:
    from ..music import AUDIO_EXTS, MUSIC_DIR
    name = Path(file.filename or "track.mp3").name
    if Path(name).suffix.lower() not in AUDIO_EXTS:
        raise HTTPException(400, f"unsupported audio type; use one of {', '.join(AUDIO_EXTS)}")
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(400, "file too large (60 MB max)")
    (MUSIC_DIR / name).write_bytes(data)
    return get_music()


@app.delete("/api/music/{file_name}")
def remove_music(file_name: str) -> list[dict[str, Any]]:
    from ..music import delete_track
    if not delete_track(file_name):
        raise HTTPException(404, "no such track")
    return get_music()


# ------------------------------------------------------------------------------------ runs and outputs

def _run_summary(run_dir: Path) -> dict[str, Any]:
    shorts = load_json(run_dir / "shorts.json", []) or []
    analysis = load_json(run_dir / "analysis.json")
    return {
        "id": run_dir.name,
        "shorts": len(shorts),
        "judged": sum(1 for s in shorts if s.get("judgment")),
        "ranked": bool(shorts) and "score" in shorts[0],
        "blueprints": len(analysis["blueprints"]) if analysis else 0,
        "has_analysis": analysis is not None,
    }


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    if not RUNS_DIR.exists():
        return []
    runs = sorted((p for p in RUNS_DIR.iterdir() if p.is_dir() and (p / "shorts.json").exists()), reverse=True)
    return [_run_summary(p) for p in runs]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, top: int = 40) -> dict[str, Any]:
    run_dir = RUNS_DIR / run_id
    if not (run_dir / "shorts.json").exists():
        raise HTTPException(404, "no such run")
    shorts = load_json(run_dir / "shorts.json", []) or []
    slim = []
    for s in shorts[:top]:
        j = s.get("judgment") or {}
        slim.append({
            "id": s["id"], "title": s["title"], "url": s["url"], "channel": s.get("channel", ""),
            "views": s.get("view_count", 0), "views_per_hour": s.get("views_per_hour"), "duration": s.get("duration"),
            "score": s.get("score"), "excluded": s.get("excluded", []), "signals": s.get("signals"),
            "format": j.get("format", {}).get("choice"), "topic": j.get("topic", {}).get("choice"),
            "hook_style": j.get("hook_style", {}).get("choice"),
            "format_confidence": j.get("format", {}).get("confidence"),
        })
    report = (run_dir / "analysis.md").read_text(encoding="utf-8") if (run_dir / "analysis.md").exists() else ""
    return {"summary": _run_summary(run_dir), "shorts": slim, "analysis": load_json(run_dir / "analysis.json"),
            "report": report}


@app.get("/api/outputs")
def list_outputs() -> list[dict[str, Any]]:
    if not OUTPUT_DIR.exists():
        return []
    out = []
    for d in sorted((p for p in OUTPUT_DIR.iterdir() if p.is_dir()), reverse=True):
        meta = load_json(d / "meta.json")
        script = load_json(d / "script.json")
        if not meta:
            continue
        qa = (script or {}).get("qa") or {}
        out.append({
            "dir": d.name, "title": meta.get("title"), "description": meta.get("description"),
            "hashtags": meta.get("hashtags", []), "duration": meta.get("duration"),
            "blueprint": {k: meta.get("blueprint", {}).get(k) for k in ("format", "topic", "hook_style")},
            "video_url": f"/outputs/{d.name}/short.mp4" if (d / "short.mp4").exists() else None,
            "thumbnail_url": f"/outputs/{d.name}/thumbnail.jpg" if (d / "thumbnail.jpg").exists() else None,
            "thumbnail_text": meta.get("thumbnail_text"),
            "youtube_id": meta.get("youtube_id"),
            "privacy": meta.get("privacy"),
            "channel_stats": meta.get("channel_stats"),
            "mode": meta.get("mode", "blueprint"),
            "music": meta.get("music"),
            "intro": meta.get("intro"), "outro": meta.get("outro"),
            "folder": str(d),
            "script_text": (script or {}).get("full_text"),
            "original_text": (script or {}).get("original_text"),
            "backend": (script or {}).get("backend"),
            "qa": {"hook_strength": qa.get("hook_strength", {}).get("score"), "clarity": qa.get("clarity", {}).get("score"),
                   "matches_format": qa.get("matches_format", {}).get("noul"), "has_payoff": qa.get("has_payoff", {}).get("noul"),
                   "policy_risk": qa.get("policy_risk", {}).get("noul"),
                   "faithful": qa.get("faithful", {}).get("noul")} if qa else None,
        })
    return out


def _channel_configured() -> bool:
    from ..channel import configured
    return configured()


def _ai_images_on() -> bool:
    from ..imagegen import available, settings as ai_settings
    s = ai_settings()
    return bool(s.get("enabled")) and s.get("mode") != "never" and available(s.get("provider"))


@app.get("/api/status")
def status() -> dict[str, Any]:
    values = _read_env()
    runs = list_runs()
    from ..tools import find_ffmpeg
    ffmpeg_dir = find_ffmpeg()
    return {
        "ffmpeg": str(ffmpeg_dir) if ffmpeg_dir else None,
        "typesafe": bool(values.get("TYPESAFE_API_KEY")),
        "script_backend": "api" if values.get("ANTHROPIC_API_KEY") else ("claude_code" if values.get("CLAUDE_CODE_OAUTH_TOKEN") else None),
        "pexels": bool(values.get("PEXELS_API_KEY")),
        "ai_images": _ai_images_on(),
        "youtube_upload": (ROOT / (values.get("YOUTUBE_CLIENT_SECRETS") or "client_secrets.json")).exists(),
        "music_tracks": len(get_music()),
        "telegram": bool(values.get("TELEGRAM_BOT_TOKEN") and values.get("TELEGRAM_CHAT_ID")),
        "channel": _channel_configured(),
        "schedule": {k: v for k, v in SCHEDULER.plan().items() if k in ("next", "done_today")} | {"enabled": bool(load_config().get("schedule", {}).get("enabled"))},
        "latest_run": runs[0] if runs else None,
        "outputs": len(list_outputs()),
        "job_running": _CURRENT.to_dict(tail=1) if _CURRENT else None,
    }


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
(ROOT / "assets" / "music").mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
app.mount("/music", StaticFiles(directory=str(ROOT / "assets" / "music")), name="music")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


@app.exception_handler(Exception)
async def _unhandled(_, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


class _QuietConnectionResets(logging.Filter):
    """Windows' proactor loop logs every client that drops a keep-alive connection as an ERROR."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "_call_connection_lost" not in record.getMessage() and "ConnectionResetError" not in record.getMessage()


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn
    logging.getLogger("asyncio").addFilter(_QuietConnectionResets())
    print(f"Shorts console: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
