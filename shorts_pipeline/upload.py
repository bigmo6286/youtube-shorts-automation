"""Upload a rendered Short with the YouTube Data API v3 (OAuth installed-app flow)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ROOT, env

log = logging.getLogger(__name__)

# Full YouTube scope: upload, read the channel's stats, and change a video's visibility after review.
SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_PATH = DATA_DIR / "youtube_token.json"

SETUP_HELP = """YouTube upload is not configured yet. One-time setup:
  1. https://console.cloud.google.com  -> create a project -> APIs & Services -> Enable "YouTube Data API v3"
  2. OAuth consent screen -> External -> add your Google account as a test user
  3. Credentials -> Create credentials -> OAuth client ID -> Desktop app -> download JSON
  4. Save it as client_secrets.json in the project folder (or set YOUTUBE_CLIENT_SECRETS in .env)
  5. Run `python main.py upload <video.mp4>` once; a browser window asks you to approve; the token is cached.
"""


def secrets_path() -> Path:
    secrets = Path(env("YOUTUBE_CLIENT_SECRETS", "client_secrets.json"))
    return secrets if secrets.is_absolute() else ROOT / secrets


def oauth_available() -> bool:
    """A client file or a cached token exists, so OAuth calls are possible (a consent may still be needed)."""
    return secrets_path().exists() or TOKEN_PATH.exists()


def _credentials(interactive: bool = True):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = secrets_path()
    creds = None
    if TOKEN_PATH.exists():
        try:
            saved = set(json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("scopes") or [])
        except (ValueError, OSError):
            saved = set()
        if set(SCOPES).issubset(saved):
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        else:
            log.info("stored YouTube token lacks %s; a new consent is needed", sorted(set(SCOPES) - saved))
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
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, authorization_prompt_message=
                                      "\nApprove access in the browser window that just opened (URL: {url})\n")
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def youtube_service(interactive: bool = True):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_credentials(interactive=interactive))


def set_privacy(video_id: str, privacy: str) -> dict[str, Any]:
    """Change an uploaded video's visibility: private | unlisted | public."""
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("privacy must be private, unlisted or public")
    youtube = youtube_service()
    return youtube.videos().update(part="status", body={"id": video_id, "status": {"privacyStatus": privacy,
                                                                                 "selfDeclaredMadeForKids": False}}).execute()


def video_status(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    youtube = youtube_service(interactive=False)
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(video_ids), 50):
        r = youtube.videos().list(part="status,statistics", id=",".join(video_ids[i:i + 50])).execute()
        for v in r.get("items", []):
            out[v["id"]] = {"privacy": v["status"].get("privacyStatus"), "upload": v["status"].get("uploadStatus"),
                            "views": int(v.get("statistics", {}).get("viewCount", 0))}
    return out


def set_thumbnail(video_id: str, image_path: Path) -> None:
    from googleapiclient.http import MediaFileUpload

    youtube = youtube_service()
    youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(image_path), mimetype="image/jpeg")).execute()


def upload_video(video_path: Path, *, title: str, description: str, tags: list[str],
                 privacy: str = "private", category_id: str = "22") -> dict[str, Any]:
    from googleapiclient.http import MediaFileUpload

    youtube = youtube_service()
    body = {
        "snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:30],
                    "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("upload %d%%", int(status.progress() * 100))
    return response
