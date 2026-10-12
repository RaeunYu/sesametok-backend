"""공용 테스트 픽스처.

모든 테스트는 합성 데이터와 임시 데이터 디렉터리에서 실행된다. 어떤 테스트도
실제 카카오톡 데이터베이스, 실제 아카이브, 사용자의 키체인에 접근하지 않는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``SESAMETOK_*`` 경로를 *tmp_path* 안으로 격리한다."""

    data = tmp_path / "data"
    logs = tmp_path / "logs"
    monkeypatch.setenv("SESAMETOK_DATA_DIR", str(data))
    monkeypatch.setenv("SESAMETOK_LOG_DIR", str(logs))
    # 테스트가 실제 ~/Library/LaunchAgents에 기록하지 않게 한다.
    monkeypatch.setenv("SESAMETOK_LAUNCHD_PLIST", str(tmp_path / "agent.plist"))
    # 단위 테스트에는 리스닝 소켓이 필요 없다.
    monkeypatch.setenv("SESAMETOK_API", "0")
    monkeypatch.delenv("SESAMETOK_API_TOKEN", raising=False)
    monkeypatch.delenv("SESAMETOK_ARCHIVE_KEY", raising=False)
    monkeypatch.delenv("SESAMETOK_USER_ID", raising=False)
    return tmp_path
