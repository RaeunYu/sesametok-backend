"""로그인 자동 시작 토글 배선 (메뉴 바 체크박스 ↔ LaunchAgent).

실제 launchd는 절대 건드리지 않는다: bootstrap/bootout은 스텁이고, plist는 ``env``
픽스처가 제공하는 임시 디렉터리에 있다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sesametok_backend import launcher
from sesametok_backend.config import Settings
from sesametok_backend.core.archive import Archive
from sesametok_backend.kakao.auth import AccountIdentity
from sesametok_backend.kakao.reader import Snapshot
from sesametok_backend.runtime import Runtime
from sesametok_backend.security.archive_key import ArchiveKey
from support import SYNTHETIC_UUID

KEY = bytes.fromhex("ab" * 32)
USER_ID = 123456789


class QuietSource:
    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        return Snapshot(rooms=(), messages=(), skipped_rows=0, open_chat_ids=frozenset())


def _runtime(settings: Settings) -> Runtime:
    return Runtime(
        settings,
        identity_resolver=lambda _s: AccountIdentity(USER_ID, SYNTHETIC_UUID, "test"),
        archive_key_resolver=lambda _s: ArchiveKey(key=KEY, source="env"),
        source=QuietSource(),
        interactive=False,
    )


@pytest.fixture
def stubbed_launchctl(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        launcher.LaunchAgent,
        "bootstrap",
        lambda self: (calls.append("bootstrap"), SimpleNamespace(ok=True, message=""))[1],
    )
    monkeypatch.setattr(
        launcher.LaunchAgent,
        "bootout",
        lambda self: (calls.append("bootout"), SimpleNamespace(ok=True, message=""))[1],
    )
    return calls


def test_autostart_starts_disabled(env) -> None:
    settings = Settings.from_env()
    assert _runtime(settings).autostart_enabled() is False


def test_toggle_installs_then_removes_the_plist(env, stubbed_launchctl) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)

    assert runtime.toggle_autostart() is True
    assert settings.launchd_plist_path.exists()
    assert runtime.autostart_enabled() is True
    assert stubbed_launchctl == ["bootstrap"]

    assert runtime.toggle_autostart() is False
    assert not settings.launchd_plist_path.exists()
    assert runtime.autostart_enabled() is False
    # 비활성화는 plist만 제거하고, 실행 중인 작업을 bootout하지는 않는다.
    assert stubbed_launchctl == ["bootstrap"]


def test_failed_bootstrap_is_reported_and_rolled_back(env, monkeypatch) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)
    monkeypatch.setattr(
        launcher.LaunchAgent,
        "bootstrap",
        lambda self: SimpleNamespace(ok=False, message="Bootstrap failed: 5"),
    )

    assert runtime.toggle_autostart() is None
    assert not settings.launchd_plist_path.exists()


def test_disallowed_plist_path_refuses_the_toggle(env, monkeypatch) -> None:
    # ~/Library/LaunchAgents 밖이고 임시 루트 밖이다.
    monkeypatch.setenv("SESAMETOK_LAUNCHD_PLIST", str(Path.home() / ".config" / "x.plist"))
    settings = Settings.from_env()
    runtime = _runtime(settings)

    assert runtime.toggle_autostart() is None
    assert runtime.autostart_enabled() is False


def test_plist_contains_the_launch_arguments(env, stubbed_launchctl) -> None:
    import plistlib

    settings = Settings.from_env()
    _runtime(settings).toggle_autostart()

    with settings.launchd_plist_path.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["Label"] == "com.sesametok.sesametok-backend"
    assert payload["ProgramArguments"][-2:] == ["foreground", "--managed"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True


def test_menu_quit_boots_out_when_managed(env, monkeypatch) -> None:
    """관리되는 프로세스는 bootout해야 한다. 아니면 KeepAlive가 다시 시작한다."""

    settings = Settings.from_env()
    runtime = Runtime(
        settings,
        identity_resolver=lambda _s: AccountIdentity(USER_ID, SYNTHETIC_UUID, "test"),
        archive_key_resolver=lambda _s: ArchiveKey(key=KEY, source="env"),
        source=QuietSource(),
        interactive=False,
        managed=True,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        launcher.LaunchAgent,
        "bootout",
        lambda self: (calls.append("bootout"), SimpleNamespace(ok=True, message=""))[1],
    )

    runtime._menu_quit()

    assert calls == ["bootout"]
    assert runtime.stopping is True


def test_menu_quit_does_not_touch_launchd_when_unmanaged(env, monkeypatch) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)  # managed의 기본값은 False
    monkeypatch.setattr(
        launcher.LaunchAgent,
        "bootout",
        lambda self: pytest.fail("bootout must not run for an unmanaged process"),
    )

    runtime._menu_quit()

    assert runtime.stopping is True


def test_menu_quit_survives_a_launchctl_failure(env, monkeypatch) -> None:
    settings = Settings.from_env()
    runtime = Runtime(
        settings,
        identity_resolver=lambda _s: AccountIdentity(USER_ID, SYNTHETIC_UUID, "test"),
        archive_key_resolver=lambda _s: ArchiveKey(key=KEY, source="env"),
        source=QuietSource(),
        interactive=False,
        managed=True,
    )
    monkeypatch.setattr(
        launcher.LaunchAgent,
        "bootout",
        lambda self: (_ for _ in ()).throw(RuntimeError("launchctl exploded")),
    )

    runtime._menu_quit()  # 그래도 중지를 요청해야 한다

    assert runtime.stopping is True
