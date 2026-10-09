"""Test setup: no network, no API keys, and no access to the real channel data.

Everything is imported under a throwaway channel (channels/pytest-tmp), API keys are blanked so nothing calls
TypeSafe, Claude, YouTube or Telegram, and tests redirect module-level state files into tmp_path.
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["SHORTS_CHANNEL"] = "pytest-tmp"
for key in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "YOUTUBE_API_KEY", "PEXELS_API_KEY", "HF_TOKEN", "OPENAI_API_KEY", "TOGETHER_API_KEY"):
    os.environ[key] = ""
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _remove_test_channel():
    yield
    shutil.rmtree(ROOT / "channels" / "pytest-tmp", ignore_errors=True)
    try:
        (ROOT / "channels").rmdir()          # only when nothing else lives there
    except OSError:
        pass


@pytest.fixture
def no_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
