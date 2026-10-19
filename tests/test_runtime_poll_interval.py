"""메뉴바의 "동기화 주기" 선택이 저장되고 폴러에 즉시 반영되는지 검증한다.

실제 메뉴바(AppKit)는 띄우지 않는다. 선택이 지나가는 경로는
``Runtime.poll_interval``/``Runtime.set_poll_interval``이며, 그 아래의 저장소
(:mod:`sesametok_backend.core.preferences`)와 폴러의 대기 주기를 직접 검사한다.
헤드리스(``SESAMETOK_NO_MENUBAR=1``) 실행에서도 저장값이 적용되는지가 핵심이다.
"""

from __future__ import annotations

import json
import logging
import stat
import threading
import time
from dataclasses import replace

import pytest

from sesametok_backend.config import Settings
from sesametok_backend.core import preferences
from sesametok_backend.kakao.auth import AccountIdentity
from sesametok_backend.kakao.reader import Snapshot
from sesametok_backend.runtime import Runtime
from sesametok_backend.security.archive_key import ArchiveKey
from support import SYNTHETIC_UUID

KEY = bytes.fromhex("ab" * 32)
USER_ID = 123456789


class QuietSource:
    def __init__(self) -> None:
        self.calls = 0

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        self.calls += 1
        return Snapshot(rooms=(), messages=(), skipped_rows=0, open_chat_ids=frozenset())


def _store(data_dir) -> preferences.Preferences:
    return preferences.Preferences.for_data_dir(data_dir)


def _settings(**overrides) -> Settings:
    """헤드리스 설정. 기본 설정 주기는 선택지와 겹치지 않게 아주 짧게 둔다."""

    base = dict(poll_seconds=0.02, no_menubar=True)
    base.update(overrides)
    return replace(Settings.from_env(), **base)


def _runtime(settings: Settings) -> Runtime:
    return Runtime(
        settings,
        identity_resolver=lambda _s: AccountIdentity(USER_ID, SYNTHETIC_UUID, "test"),
        archive_key_resolver=lambda _s: ArchiveKey(key=KEY, source="env"),
        source=QuietSource(),
        interactive=False,
    )


def _drive(runtime: Runtime, action, *, timeout: float = 5.0) -> dict:
    """런타임을 스레드에서 돌리고, 폴러가 생기면 *action(runtime)*을 실행한 뒤 멈춘다."""

    result: dict = {}

    def driver() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and runtime.poller is None and not runtime.stopping:
            time.sleep(0.01)
        try:
            result.update(action(runtime))
        finally:
            runtime.request_stop()

    worker = threading.Thread(target=driver, daemon=True)
    worker.start()
    try:
        assert runtime.run_foreground(install_signal_handlers=False) == 0
    finally:
        worker.join(timeout=timeout)
    return result


# ------------------------------------------------------------------ 저장소


def test_poll_seconds_round_trip(tmp_path) -> None:
    store = _store(tmp_path)

    assert store.poll_seconds() is None
    assert store.set_poll_seconds(10) == 10.0
    assert store.poll_seconds() == 10.0
    assert store.set_poll_seconds(2) == 2.0
    assert store.poll_seconds() == 2.0

    # 파일에는 주기 숫자 하나만 있고, 권한은 0600이며, 임시 파일이 남지 않는다.
    assert json.loads(store.path.read_text(encoding="utf-8")) == {"poll_seconds": 2.0}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert [path.name for path in tmp_path.iterdir()] == ["preferences.json"]


def test_missing_store_is_not_an_error(tmp_path) -> None:
    store = _store(tmp_path / "data")
    assert store.path.name == "preferences.json"
    assert store.poll_seconds() is None
    assert store.read() == {}


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[]",
        '"10"',
        "{}",
        '{"poll_seconds": 0}',
        '{"poll_seconds": -2}',
        '{"poll_seconds": "10"}',
        '{"poll_seconds": null}',
        '{"poll_seconds": true}',
        '{"poll_seconds": Infinity}',
        '{"poll_seconds": NaN}',
    ],
)
def test_invalid_stored_values_are_ignored(tmp_path, payload: str) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(payload, encoding="utf-8")
    store = preferences.Preferences(path)

    assert store.poll_seconds() is None
    # 그래서 설정값이 쓰인다.
    assert preferences.resolve_poll_seconds(store.poll_seconds(), 1.5) == 1.5


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "5", True, None])
def test_set_poll_seconds_refuses_invalid_values(tmp_path, bad) -> None:
    store = _store(tmp_path)
    assert store.set_poll_seconds(bad) is None
    assert store.poll_seconds() is None


def test_resolution_priority_is_stored_then_settings_then_default() -> None:
    assert preferences.resolve_poll_seconds(10.0, 5.0) == 10.0
    assert preferences.resolve_poll_seconds(None, 5.0) == 5.0
    assert preferences.resolve_poll_seconds(0, 5.0) == 5.0
    assert preferences.resolve_poll_seconds(-1, 0) == 3.0
    assert preferences.resolve_poll_seconds(None, None) == 3.0
    assert preferences.resolve_poll_seconds(float("nan"), float("inf")) == 3.0


def test_a_failed_save_is_logged_but_not_raised(tmp_path, caplog) -> None:
    target = tmp_path / "preferences.json"
    target.mkdir()  # 디렉터리라서 os.replace가 실패한다
    store = preferences.Preferences(target)

    with caplog.at_level(logging.WARNING, logger="sesametok_backend.core.preferences"):
        assert store.set_poll_seconds(10) is None

    assert store.poll_seconds() is None
    assert any("preferences.json" in record.getMessage() for record in caplog.records)


# ------------------------------------------------------------- 런타임 연결


def test_headless_runtime_applies_the_stored_interval(env) -> None:
    """요구 5: 메뉴바가 없어도 저장된 주기가 폴러에 적용된다."""

    settings = _settings()
    assert _store(settings.data_dir).set_poll_seconds(60) == 60.0

    runtime = _runtime(settings)
    seen = _drive(runtime, lambda rt: {"interval": rt.poller.interval if rt.poller else None})

    assert seen["interval"] == 60.0


def test_settings_interval_is_used_without_a_stored_value(env) -> None:
    settings = _settings(poll_seconds=7)

    runtime = _runtime(settings)
    seen = _drive(runtime, lambda rt: {"interval": rt.poller.interval if rt.poller else None})

    assert seen["interval"] == 7.0


def test_selecting_an_interval_applies_immediately_and_persists(env) -> None:
    settings = _settings(poll_seconds=0.02)

    def choose(runtime: Runtime) -> dict:
        before = runtime.poll_interval()
        applied = runtime.set_poll_interval(30)
        return {"before": before, "applied": applied, "after": runtime.poller.interval}

    seen = _drive(_runtime(settings), choose)

    assert seen["before"] == 0.02
    assert seen["applied"] is True
    assert seen["after"] == 30.0  # 재시작 없이 즉시 반영
    assert _store(settings.data_dir).poll_seconds() == 30.0  # 다음 실행에도 유지


def test_a_failed_save_does_not_block_the_interval_change(env) -> None:
    settings = _settings()
    (settings.data_dir / "preferences.json").mkdir(parents=True)  # 저장을 실패시킨다

    seen = _drive(
        _runtime(settings),
        lambda rt: {
            "applied": rt.set_poll_interval(15),
            "after": rt.poller.interval,
        },
    )

    assert seen["applied"] is True
    assert seen["after"] == 15.0
    assert (settings.data_dir / "preferences.json").is_dir()


def test_set_poll_interval_refuses_invalid_values_before_start(env) -> None:
    runtime = _runtime(_settings())

    for bad in (0, -1, float("nan"), "10", True, None):
        assert runtime.set_poll_interval(bad) is False

    # 폴러가 없어도 유효한 값은 저장하고, 그것이 다음 주기가 된다.
    assert runtime.set_poll_interval(10) is True
    assert _store(runtime.settings.data_dir).poll_seconds() == 10.0
    assert runtime.poll_interval() == 10.0
