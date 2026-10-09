"""Upload a rendered Short with the YouTube Data API v3 (OAuth installed-app flow)."""
from __future__ import annotations

import json
import logging
import socket
import ssl
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from .config import DATA_DIR, ROOT, env

log = logging.getLogger(__name__)

# Full YouTube scope: upload, read the channel's stats, and change a video's visibility after review.
SCOPES = ["https://www.googleapis.com/auth/youtube"]
# Read-only YouTube Analytics (retention: average % viewed, view duration, engaged views). Optional: uploads never
# require it, so a token granted before this existed keeps working; every new consent asks for both.
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
CONSENT_SCOPES = SCOPES + [ANALYTICS_SCOPE]
T = TypeVar("T")
BACKOFF_SECONDS = [5, 15, 30, 60, 120, 180]     # ~7 minutes in total before an upload step gives up


def is_transient(exc: BaseException) -> bool:
    """A failure worth retrying: network drops, DNS hiccups, timeouts, and Google's own 5xx errors.
    Never the upload limit, an exhausted quota or an expired sign-in."""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            status = int(getattr(exc.resp, "status", 0) or 0)
            return status >= 500 or "backendError" in str(exc)
    except ImportError:
        pass
    name = type(exc).__name__
    if name in ("ServerNotFoundError", "TransportError", "RedirectMissingLocation", "IncompleteRead"):
        return True
    if isinstance(exc, (TimeoutError, socket.timeout, socket.gaierror, ConnectionError, ssl.SSLError, BrokenPipeError)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054, 10060, 10065, 11001, 11002):
        return True
    text = str(exc)
    return any(k in text for k in ("Unable to find the server", "timed out", "Connection reset", "Connection aborted",
                                   "EOF occurred", "Max retries exceeded", "RemoteDisconnected"))


def with_retries(fn: Callable[[], T], what: str) -> T:
    """Run `fn`, retrying transient failures with backoff."""
    for attempt, pause in enumerate([*BACKOFF_SECONDS, None], 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if pause is None or not is_transient(exc):
                raise
            log.warning("%s: %s; retrying in %d s (attempt %d of %d)", what, str(exc)[:120], pause, attempt,
                        len(BACKOFF_SECONDS))
            time.sleep(pause)
    raise AssertionError("unreachable")
TOKEN_PATH = DATA_DIR / "youtube_token.json"

SETUP_HELP = """YouTube upload is not configured yet. One-time setup:
  1. https://console.cloud.google.com  -> create a project -> APIs & Services -> Enable "YouTube Data API v3"
  2. OAuth consent screen -> External -> add your Google account as a test user
  3. Credentials -> Create credentials -> OAuth client ID -> Desktop app -> download JSON
  4. Save it as client_secrets.json in the project folder (or set YOUTUBE_CLIENT_SECRETS in .env)
  5. Run `python main.py upload <video.mp4>` once; a browser window asks you to approve; the token is cached.
"""


def secrets_path() -> Path:
    """The OAuth client file: absolute, or relative to this channel's folder, then to the install folder."""
    from .config import HOME
    secrets = Path(env("YOUTUBE_CLIENT_SECRETS", "client_secrets.json"))
    if secrets.is_absolute():
        return secrets
    return HOME / secrets if (HOME / secrets).exists() or HOME != ROOT else ROOT / secrets


def oauth_available() -> bool:
    """A client file or a cached token exists, so OAuth calls are possible (a consent may still be needed)."""
    return secrets_path().exists() or TOKEN_PATH.exists()


def saved_scopes() -> set[str]:
    try:
        return set(json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("scopes") or [])
    except (ValueError, OSError):
        return set()


def has_scope(scope: str) -> bool:
    return scope in saved_scopes()


def _credentials(interactive: bool = True, extra_scopes: tuple[str, ...] = ()):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = secrets_path()
    creds = None
    need = set(SCOPES) | set(extra_scopes)
    if TOKEN_PATH.exists():
        saved = saved_scopes()
        if need.issubset(saved):
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), sorted(saved))
        else:
            log.info("stored YouTube token lacks %s; a new consent is needed", sorted(need - saved))
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        except RefreshError as exc:
            # Google expires the refresh tokens of OAuth apps still in "Testing" status after 7 days
            # (and whenever you revoke access). Drop the dead token and ask for consent again.
            log.warning("stored YouTube token is no longer valid (%s); asking for consent again. To stop this "
                        "happening every 7 days, set the OAuth consent screen's publishing status to 'In production' "
                        "in the Google Cloud console (unverified is fine for your own channel).", str(exc)[:120])
            creds = None
            try:
                TOKEN_PATH.unlink()
            except OSError:
                pass
    if not creds or not creds.valid:
        if not secrets.exists():
            raise FileNotFoundError(SETUP_HELP)
        if not interactive:
            raise RuntimeError("YouTube access needs a new consent: run an upload or 'Sync channel now' from the console "
                               "(or `python main.py channel sync` in a terminal) and approve the browser prompt.")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), CONSENT_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, authorization_prompt_message=
                                      "\nApprove access in the browser window that just opened (URL: {url})\n")
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(creds.to_json())
        granted = getattr(creds, "granted_scopes", None)
        if granted:                       # the owner may untick Analytics on Google's page: record what was granted
            data["scopes"] = sorted(granted)
        TOKEN_PATH.write_text(json.dumps(data), encoding="utf-8")
        missing = need - set(data.get("scopes") or [])
        if missing:
            raise RuntimeError(f"Google did not grant {sorted(missing)}; approve every permission on the consent page.")
    return creds


def youtube_service(interactive: bool = True):
    from googleapiclient.discovery import build
    # static_discovery: use the API description bundled with the client instead of downloading it (~13 s) each time
    return build("youtube", "v3", credentials=_credentials(interactive=interactive), static_discovery=True)


def set_privacy(video_id: str, privacy: str) -> dict[str, Any]:
    """Change an uploaded video's visibility: private | unlisted | public."""
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("privacy must be private, unlisted or public")
    youtube = with_retries(youtube_service, "connect to YouTube")
    return with_retries(lambda: youtube.videos().update(part="status", body={"id": video_id, "status": {
        "privacyStatus": privacy, "selfDeclaredMadeForKids": False}}).execute(), "change privacy")


def video_status(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    youtube = with_retries(lambda: youtube_service(interactive=False), "connect to YouTube")
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(video_ids), 50):
        batch = ",".join(video_ids[i:i + 50])
        r = with_retries(lambda: youtube.videos().list(part="status,statistics", id=batch).execute(), "read video status")
        for v in r.get("items", []):
            out[v["id"]] = {"privacy": v["status"].get("privacyStatus"), "upload": v["status"].get("uploadStatus"),
                            "publish_at": v["status"].get("publishAt"),
                            "views": int(v.get("statistics", {}).get("viewCount", 0))}
    return out


def sync_output_status(output_dir: Path) -> int:
    """Ask YouTube for the current privacy of every uploaded Short in `output_dir` and write it to its meta.json,
    so a change made in YouTube Studio shows in the console. A video that no longer exists is marked 'deleted'.
    Costs 1 API quota unit per 50 videos. Never opens a browser (raises if consent is needed). Returns changes."""
    from .storage import load_json, save_json

    metas: dict[str, Path] = {}
    if output_dir.exists():
        for d in output_dir.iterdir():
            meta = load_json(d / "meta.json") if d.is_dir() else None
            if meta and meta.get("youtube_id"):
                metas[meta["youtube_id"]] = d / "meta.json"
    if not metas:
        return 0
    live = video_status(list(metas))
    changed = 0
    for vid, path in metas.items():
        privacy = live[vid]["privacy"] if vid in live else "deleted"
        publish_at = live[vid].get("publish_at") if vid in live else None
        meta = load_json(path)                 # re-read right before writing: a job may have updated it meanwhile
        if not meta or meta.get("youtube_id") != vid or (meta.get("privacy") == privacy
                                                         and meta.get("publish_at") == publish_at):
            continue
        if meta.get("privacy") != privacy:
            log.info("%s is now %s on YouTube (console had %s)", vid, privacy, meta.get("privacy"))
        meta["privacy"] = privacy
        meta["publish_at"] = publish_at        # cleared when it went public or the schedule was cancelled in Studio
        save_json(path, meta)
        changed += 1
    return changed


def set_thumbnail(video_id: str, image_path: Path) -> None:
    from googleapiclient.http import MediaFileUpload

    youtube = with_retries(youtube_service, "connect to YouTube")
    with_retries(lambda: youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(
        str(image_path), mimetype="image/jpeg")).execute(), "set thumbnail")


def upload_video(video_path: Path, *, title: str, description: str, tags: list[str],
                 privacy: str = "private", category_id: str = "22", interactive: bool = True,
                 publish_at: str | None = None) -> dict[str, Any]:
    """Resumable upload. A dropped connection resumes from the last confirmed chunk instead of starting over or
    failing; up to len(BACKOFF_SECONDS) consecutive transient failures are tolerated. `interactive=False` (the
    automatic queue) never opens a browser for consent; it raises so the owner gets an alert instead."""
    from googleapiclient.http import MediaFileUpload

    youtube = with_retries(lambda: youtube_service(interactive=interactive), "connect to YouTube")
    body = {
        "snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:30],
                    "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    if publish_at:                        # YouTube makes it public by itself at this time (must start private)
        body["status"].update(privacyStatus="private", publishAt=publish_at)
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    failures = 0
    while response is None:
        try:
            status, response = request.next_chunk()
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc) or failures >= len(BACKOFF_SECONDS):
                raise
            pause = BACKOFF_SECONDS[failures]
            failures += 1
            log.warning("upload interrupted (%s); resuming in %d s (attempt %d of %d)", str(exc)[:120], pause,
                        failures, len(BACKOFF_SECONDS))
            time.sleep(pause)
            continue
        failures = 0                      # progress was made: the next drop gets the full retry budget again
        if status:
            log.info("upload %d%%", int(status.progress() * 100))
    return response
