"""Upload a rendered Short with the YouTube Data API v3 (OAuth installed-app flow)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ROOT, env

log = logging.getLogger(__name__)

# upload + read-only: one consent covers uploading Shorts and reading your channel's stats for feedback
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
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
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = secrets_path()
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds and not set(SCOPES).issubset(set(creds.scopes or [])):
            creds = None                           # token from before the read-only scope was added: re-consent
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        if not secrets.exists():
            raise FileNotFoundError(SETUP_HELP)
        if not interactive:
            raise RuntimeError("YouTube access is not authorised yet: run `python main.py channel sync` (or an upload) "
                               "once from a terminal and approve the browser prompt.")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, authorization_prompt_message=
                                      "\nApprove access in the browser window that just opened (URL: {url})\n")
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def youtube_service(interactive: bool = True):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_credentials(interactive=interactive))


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
