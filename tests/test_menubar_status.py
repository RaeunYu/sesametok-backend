"""메뉴바 상태 모델과 메뉴 내용에 대한 헤드리스 테스트.

여기 어떤 테스트도 AppKit을 필요로 하지 않는다. ``app.status``는 순수하고, 뷰 계층
검사 하나는 서브프로세스 임포트 가드다. 개인 데이터는 메뉴 텍스트에 절대 나타나면
안 된다.
"""

from __future__ import annotations

import builtins
import importlib
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sesametok_backend.app import status as status_model
from sesametok_backend.app.status import (
    SF_SYMBOLS,
    ITEM_AUTOSTART,
    ITEM_INFO_STORAGE,
    ITEM_INFO_UNSUPPORTED,
    ITEM_WEBHOOK_STATUS,
    Status,
    build_snapshot,
    compute_state,
    menu_items,
    relative_time,
    summary_text,
    symbol_name,
)

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)

#: 임포트 가드가 차단해 AppKit 없는 머신을 흉내 내는 최상위 모듈들.
_BLOCKED_MODULES = ("AppKit", "Foundation", "objc", "PyObjCTools", "Quartz", "ApplicationServices")


def _state(**overrides) -> Status:
    inputs: dict = dict(
        kakaotalk_running=True,
        permissions_ok=True,
        polling=True,
        paused=False,
        sending=False,
        consecutive_failures=0,
        last_sync_at=NOW - timedelta(seconds=3),
        starting=False,
    )
    inputs.update(overrides)
    return compute_state(**inputs)


def _snapshot(**overrides):
    inputs: dict = dict(
        kakaotalk_running=True,
        permissions_ok=True,
        polling=True,
        paused=False,
        sending=False,
        consecutive_failures=0,
        last_sync_at=NOW - timedelta(seconds=3),
        starting=False,
        now=NOW,
    )
    inputs.update(overrides)
    return build_snapshot(**inputs)


# --------------------------------------------------------------- 우선순위


def test_ok_is_the_quiet_default() -> None:
    assert _state() is Status.ok


def test_error_beats_degraded() -> None:
    assert _state(consecutive_failures=5, permissions_ok=False) is Status.error


def test_degraded_beats_sending_and_paused() -> None:
    assert _state(permissions_ok=False, sending=True, paused=True) is Status.degraded


def test_sending_beats_paused() -> None:
    assert _state(sending=True, paused=True, polling=False) is Status.sending


def test_paused_beats_starting_and_ok() -> None:
    assert _state(paused=True, polling=False, starting=True) is Status.paused


def test_starting_beats_ok_and_a_missing_first_sync() -> None:
    assert _state(starting=True, last_sync_at=None) is Status.starting


def test_kakaotalk_stopped_degrades_but_unknown_does_not() -> None:
    assert _state(kakaotalk_running=False) is Status.degraded
    assert _state(kakaotalk_running=None) is Status.ok


def test_failure_threshold_degrades_then_errors() -> None:
    assert _state(consecutive_failures=0) is Status.ok
    assert _state(consecutive_failures=1) is Status.degraded
    assert _state(consecutive_failures=2) is Status.degraded
    assert _state(consecutive_failures=3) is Status.error


def test_missing_permissions_or_stopped_polling_degrades() -> None:
    assert _state(permissions_ok=False) is Status.degraded
    assert _state(polling=False) is Status.degraded
    # 의도적인 일시정지는 성능저하가 아니다.
    assert _state(polling=False, paused=True) is Status.paused


def test_never_synced_degrades_unless_starting_or_paused() -> None:
    assert _state(last_sync_at=None) is Status.degraded
    assert _state(last_sync_at=None, starting=True) is Status.starting
    assert _state(last_sync_at=None, polling=False, paused=True) is Status.paused


# ------------------------------------------------------------------ 심볼


def test_symbol_mapping_matches_section_6_1() -> None:
    assert SF_SYMBOLS == {
        Status.starting: "clock",
        Status.ok: "bubble.left.and.bubble.right",
        Status.degraded: "exclamationmark.triangle",
        Status.paused: "pause.circle",
        Status.sending: "arrow.up.circle",
        Status.error: "xmark.octagon",
    }
    for state, symbol in SF_SYMBOLS.items():
        assert symbol_name(state) == symbol


# ------------------------------------------------------------------ 요약


def test_summary_uses_injected_now_and_counts_only() -> None:
    text = summary_text(Status.ok, room_count=42, last_sync_at=NOW - timedelta(seconds=3), now=NOW)
    assert text == "정상 · 방 42 · 최근 동기화 3초 전"
    assert re.fullmatch(r"정상 · 방 42 · 최근 동기화 \d+초 전", text)


def test_summary_accepts_rfc3339_and_epoch_timestamps() -> None:
    rfc = summary_text(
        Status.ok,
        last_sync_at="2026-01-02T11:59:57+00:00",
        now=NOW,
    )
    epoch = summary_text(Status.ok, last_sync_at=NOW.timestamp() - 3, now=NOW)
    assert rfc == epoch == "정상 · 최근 동기화 3초 전"


def test_summary_omits_unknown_fields_and_never_leaks_names() -> None:
    # 아무것도 모를 때는 상태 라벨만 나온다.
    assert summary_text(Status.paused) == "일시정지"
    # 실패 횟수는 내용이 아니라 개수다.
    text = summary_text(Status.error, consecutive_failures=7, room_count=3, last_sync_at=None)
    assert text == "오류 · 방 3 · 연속 실패 7회"
    assert "비밀방" not in text and "닉네임" not in text


def test_relative_time_boundaries() -> None:
    assert relative_time(NOW, NOW) == "0초 전"
    assert relative_time(NOW - timedelta(seconds=59), NOW) == "59초 전"
    assert relative_time(NOW - timedelta(minutes=1), NOW) == "1분 전"
    assert relative_time(NOW - timedelta(hours=1), NOW) == "1시간 전"
    assert relative_time(NOW - timedelta(days=1), NOW) == "1일 전"
    # 시계 오차가 있어도 음수 표현이 나오지 않는다.
    assert relative_time(NOW + timedelta(seconds=5), NOW) == "0초 전"


def test_build_snapshot_composes_state_and_summary() -> None:
    snapshot = _snapshot(room_count=42)
    assert snapshot.status is Status.ok
    assert snapshot.summary == "정상 · 방 42 · 최근 동기화 3초 전"


# -------------------------------------------------------------- 메뉴 내용


def _by_id(items, item_id):
    matches = [item for item in items if item.item_id == item_id]
    assert len(matches) == 1, f"expected exactly one menu item {item_id!r}"
    return matches[0]


def test_menu_includes_required_items() -> None:
    items = menu_items(_snapshot(autostart=True, room_count=42), executable_path="/tmp/python")
    ids = {item.item_id for item in items}
    for required in (
        status_model.ITEM_STATUS,
        status_model.ITEM_KAKAOTALK,
        status_model.ITEM_PERMISSIONS_FULL_DISK,
        status_model.ITEM_PERMISSIONS_ACCESSIBILITY,
        status_model.ITEM_PERMISSIONS_FULL_DISK_SETTINGS,
        status_model.ITEM_PERMISSIONS_ACCESSIBILITY_SETTINGS,
        status_model.ITEM_SYNC_NOW,
        status_model.ITEM_TOGGLE_PAUSE,
        status_model.ITEM_COPY_API_TOKEN,
        status_model.ITEM_OPEN_LOG,
        ITEM_AUTOSTART,
        ITEM_WEBHOOK_STATUS,
        status_model.ITEM_RESTART,
        status_model.ITEM_QUIT,
    ):
        assert required in ids, required

    assert _by_id(items, status_model.ITEM_STATUS).title == _snapshot(room_count=42).summary
    assert _by_id(items, status_model.ITEM_STATUS).enabled is False
    assert _by_id(items, status_model.ITEM_QUIT).action == status_model.ACTION_QUIT
    assert _by_id(items, status_model.ITEM_QUIT).title == "종료"
    assert _by_id(items, status_model.ITEM_QUIT).enabled is True


def test_permission_state_and_settings_are_short_and_separate() -> None:
    """요구 1: 권한 상태는 짧게, 설정 열기는 별도 항목, 긴 실행 파일 경로는 제거."""

    items = menu_items(_snapshot(accessibility_trusted=False), executable_path="/tmp/python")

    full_disk = _by_id(items, status_model.ITEM_PERMISSIONS_FULL_DISK)
    accessibility = _by_id(items, status_model.ITEM_PERMISSIONS_ACCESSIBILITY)
    assert full_disk.title == "카카오톡 읽기: 정상"
    assert accessibility.title == "손쉬운 사용: 필요"
    for item in (full_disk, accessibility):
        assert item.kind == status_model.KIND_INFO
        assert item.enabled is False and item.action is None
        assert "설정 열기" not in item.title
        assert "권한 · " not in item.title

    # 설정 열기는 각 권한의 기존 딥링크를 그대로 쓰는 별도 항목이다.
    fda = _by_id(items, status_model.ITEM_PERMISSIONS_FULL_DISK_SETTINGS)
    ax = _by_id(items, status_model.ITEM_PERMISSIONS_ACCESSIBILITY_SETTINGS)
    assert fda.enabled is True and fda.action == status_model.ACTION_OPEN_FULL_DISK_ACCESS
    assert ax.enabled is True and ax.action == status_model.ACTION_OPEN_ACCESSIBILITY
    assert "설정 열기" in fda.title and "설정 열기" in ax.title

    # 실행 파일 경로 같은 긴 안내는 메뉴에서 사라진다(상태 커맨드·로그가 담당).
    assert status_model.ITEM_PERMISSIONS_TARGET not in {item.item_id for item in items}
    assert not any("/tmp/python" in item.title for item in items)


def test_restart_is_disabled_with_a_reason_when_unmanaged() -> None:
    item = _by_id(menu_items(_snapshot(managed=False)), status_model.ITEM_RESTART)
    assert item.action == status_model.ACTION_RESTART
    assert item.enabled is False
    assert item.title == "재시작 · launchd로 실행 중일 때만"


def test_restart_is_enabled_when_managed() -> None:
    item = _by_id(menu_items(_snapshot(managed=True)), status_model.ITEM_RESTART)
    assert item.action == status_model.ACTION_RESTART
    assert item.enabled is True
    assert item.title == "재시작"


def test_menu_has_no_media_or_emoticon_toggle() -> None:
    items = menu_items(_snapshot())
    for item in items:
        if item.kind in (status_model.KIND_CHECKBOX, status_model.KIND_ACTION):
            assert "미디어" not in item.title
            assert "이모티콘" not in item.title
        assert item.action is None or "media" not in item.action
        assert item.action is None or "emoticon" not in item.action
    # 로그인 자동시작 체크박스가 유일한 체크박스다(ADR-007이 유지한다).
    checkboxes = [item for item in items if item.kind == status_model.KIND_CHECKBOX]
    assert [item.item_id for item in checkboxes] == [ITEM_AUTOSTART]


def test_login_autostart_checkbox_follows_injected_bool() -> None:
    assert _by_id(menu_items(_snapshot(autostart=True)), ITEM_AUTOSTART).checked is True
    assert _by_id(menu_items(_snapshot(autostart=False)), ITEM_AUTOSTART).checked is False


def test_webhook_status_is_read_only_and_shows_counts_only() -> None:
    webhooks = (
        {"id": "a", "url": "http://127.0.0.1:1/hook", "active": True,
         "last_success_at": "2026-01-02T11:59:55+00:00", "last_error": None},
        {"id": "b", "url": "http://127.0.0.1:2/hook", "active": False,
         "last_success_at": None, "last_error": "HTTPError"},
    )
    item = _by_id(menu_items(_snapshot(webhooks=webhooks), now=NOW), ITEM_WEBHOOK_STATUS)
    assert item.enabled is False
    assert item.action is None
    assert "활성 1/2개" in item.title
    assert "5초 전" in item.title
    assert "http" not in item.title and "secret" not in item.title

    empty = _by_id(menu_items(_snapshot(webhooks=())), ITEM_WEBHOOK_STATUS)
    assert empty.title == "webhook: 구독 없음"
    assert empty.enabled is False


def test_storage_and_unsupported_type_lines_are_disabled() -> None:
    items = menu_items(_snapshot())
    unsupported = _by_id(items, ITEM_INFO_UNSUPPORTED)
    storage = _by_id(items, ITEM_INFO_STORAGE)
    assert unsupported.enabled is False and unsupported.action is None
    assert storage.enabled is False and storage.action is None
    assert "치환" in unsupported.title
    assert "저장" in storage.title


def test_pause_item_toggles_title() -> None:
    assert _by_id(menu_items(_snapshot(paused=False)), status_model.ITEM_TOGGLE_PAUSE).title == "일시정지"
    assert _by_id(menu_items(_snapshot(paused=True, polling=False)), status_model.ITEM_TOGGLE_PAUSE).title == "재개"


def test_sync_now_disabled_while_paused() -> None:
    assert _by_id(menu_items(_snapshot(paused=True, polling=False)), status_model.ITEM_SYNC_NOW).enabled is False
    assert _by_id(menu_items(_snapshot()), status_model.ITEM_SYNC_NOW).enabled is True


# --------------------------------------------------------- 동기화 주기 메뉴


def test_interval_submenu_lists_five_choices_and_checks_only_the_current_one() -> None:
    container = _by_id(menu_items(_snapshot(poll_seconds=10.0)), status_model.ITEM_INTERVAL)

    assert container.title == "동기화 주기"
    assert container.action is None
    assert container.kind != status_model.KIND_SEPARATOR
    options = list(container.children)
    assert [option.title for option in options] == [
        "2초마다",
        "5초마다",
        "10초마다",
        "30초마다",
        "60초마다",
    ]
    assert [option.item_id for option in options] == [
        "interval_2",
        "interval_5",
        "interval_10",
        "interval_30",
        "interval_60",
    ]
    assert [option.checked for option in options] == [False, False, True, False, False]

    for option, seconds in zip(options, (2.0, 5.0, 10.0, 30.0, 60.0)):
        assert option.enabled is True
        assert option.action == status_model.ACTION_SET_INTERVAL
        assert option.kind == status_model.KIND_INTERVAL
        assert option.value == seconds
        assert status_model.interval_label(seconds) == option.title


def test_interval_check_follows_the_snapshot_interval() -> None:
    for seconds in status_model.INTERVAL_CHOICES:
        options = _by_id(
            menu_items(_snapshot(poll_seconds=seconds)), status_model.ITEM_INTERVAL
        ).children
        checked = [option.value for option in options if option.checked]
        assert checked == [seconds]


def test_interval_submenu_checks_nothing_for_an_unlisted_interval() -> None:
    # 설정/Settings 기본값은 3초이고 선택지에 없다: 값을 왜곡해 체크하지 않는다.
    assert _snapshot().poll_seconds == 3.0
    options = _by_id(menu_items(_snapshot()), status_model.ITEM_INTERVAL).children
    assert [option.checked for option in options] == [False] * 5


def test_interval_choices_are_not_the_autostart_checkbox() -> None:
    """기존 계약: ``KIND_CHECKBOX``는 로그인 자동시작 하나뿐이다."""

    items = menu_items(_snapshot(poll_seconds=30.0))
    checkboxes = [item for item in items if item.kind == status_model.KIND_CHECKBOX]
    assert [item.item_id for item in checkboxes] == [ITEM_AUTOSTART]
    options = _by_id(items, status_model.ITEM_INTERVAL).children
    assert all(option.kind == status_model.KIND_INTERVAL for option in options)
    # 선택지는 개인 데이터를 담지 않는다(초 숫자와 고정 라벨뿐이다).
    assert all(option.title.endswith("초마다") for option in options)


def test_invalid_snapshot_interval_falls_back_to_the_default() -> None:
    for bad in (0, -5, None, float("nan"), float("inf")):
        assert _snapshot(poll_seconds=bad).poll_seconds == 3.0


# ------------------------------------------------------------- 임포트 가드

_STATUS_IMPORT_PROBE = f"""
import builtins, sys
_real = builtins.__import__
def _guard(name, *args, **kwargs):
    if name.split(".")[0] in {_BLOCKED_MODULES!r}:
        raise ImportError("blocked: " + name)
    return _real(name, *args, **kwargs)
builtins.__import__ = _guard
from sesametok_backend.app import status
assert status.symbol_name(status.Status.ok) == "bubble.left.and.bubble.right"
assert status.compute_state(
    kakaotalk_running=True, permissions_ok=True, polling=True, paused=False,
    sending=False, consecutive_failures=0, last_sync_at=1.0, starting=False,
) is status.Status.ok
leaked = [name for name in sys.modules if name.split(".")[0] in {_BLOCKED_MODULES!r}]
assert not leaked, leaked
print("ok")
"""


def test_status_imports_without_appkit() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _STATUS_IMPORT_PROBE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_menubar_import_raises_a_clear_error_without_appkit(monkeypatch) -> None:
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in _BLOCKED_MODULES:
            raise ImportError("blocked: " + name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    sys.modules.pop("sesametok_backend.app.menubar", None)
    with pytest.raises(ImportError, match="AppKit"):
        importlib.import_module("sesametok_backend.app.menubar")


# ------------------------------------------------- 런타임 통합(순수)

_KEY = bytes.fromhex("ab" * 32)
_USER_ID = 123456789


class _QuietSource:
    def read_snapshot(self, *, since_epoch=None):  # noqa: ANN001, ANN201
        from sesametok_backend.kakao.reader import Snapshot

        return Snapshot(rooms=(), messages=(), skipped_rows=0, open_chat_ids=frozenset())


def test_run_foreground_without_menubar_uses_the_plain_wait_loop(env: Path) -> None:
    """요구사항 3: ``no_menubar``는 기존 헤드리스 대기 루프를 유지한다."""

    from dataclasses import replace

    from sesametok_backend.config import Settings
    from sesametok_backend.kakao.auth import AccountIdentity
    from sesametok_backend.runtime import Runtime
    from sesametok_backend.security.archive_key import ArchiveKey
    from support import SYNTHETIC_UUID

    settings = replace(Settings.from_env(), poll_seconds=0.01, no_menubar=True)
    runtime = Runtime(
        settings,
        identity_resolver=lambda _settings: AccountIdentity(_USER_ID, SYNTHETIC_UUID, "test"),
        archive_key_resolver=lambda _settings: ArchiveKey(key=_KEY, source="env"),
        source=_QuietSource(),
        interactive=False,
    )
    threading.Thread(target=lambda: (time.sleep(0.1), runtime.request_stop()), daemon=True).start()

    assert runtime.run_foreground(install_signal_handlers=False) == 0
    assert not settings.lock_path.exists()
