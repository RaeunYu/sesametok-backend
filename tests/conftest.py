"""Shared test fixtures.

Every test runs against synthetic data and a temporary data directory; no test
touches the real KakaoTalk database, the real archive, or the user's keychain.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``SESAMETOK_*`` paths inside *tmp_path*."""

    data = tmp_path / "data"
    logs = tmp_path / "logs"
    monkeypatch.setenv("SESAMETOK_DATA_DIR", str(data))
    monkeypatch.setenv("SESAMETOK_LOG_DIR", str(logs))
    # Never let a test write into the real ~/Library/LaunchAgents.
    monkeypatch.setenv("SESAMETOK_LAUNCHD_PLIST", str(tmp_path / "agent.plist"))
    # Unit tests do not need a listening socket.
    monkeypatch.setenv("SESAMETOK_API", "0")
    monkeypatch.delenv("SESAMETOK_API_TOKEN", raising=False)
    monkeypatch.delenv("SESAMETOK_ARCHIVE_KEY", raising=False)
    monkeypatch.delenv("SESAMETOK_USER_ID", raising=False)
    return tmp_path
