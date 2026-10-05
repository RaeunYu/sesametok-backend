"""Pure menu bar status model: states, symbols, summaries, and menu contents.

This module is deliberately **AppKit-free** so it imports and unit-tests headlessly.
It encodes the state model and the menu contents, as amended by ADR-007: the
"미디어 폴링 여부" / "이모티콘 폴링 여부" toggles are **removed** and a read-only
webhook status item is **added**.

Privacy rule: every string produced here is built from state,
counts, and timestamps only. Room names, nicknames, and message bodies never enter
menu text, tooltips, or logs.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

#: Consecutive poll failures at or above this count raise ``Status.error``;
#: anything between 1 and the threshold is ``Status.degraded``.
FAILURE_THRESHOLD = 3


class Status(enum.Enum):
    """Menu bar states, declared from highest to lowest precedence.

    Precedence: ``error`` > ``degraded`` > ``sending`` >
    ``paused`` > ``starting`` > ``ok``.
    """

    error = "error"
    degraded = "degraded"
    sending = "sending"
    paused = "paused"
    starting = "starting"
    ok = "ok"


_PRECEDENCE: dict[Status, int] = {state: rank for rank, state in enumerate(Status)}

#: Human labels for the menu.
STATE_LABELS: dict[Status, str] = {
    Status.starting: "기동 중",
    Status.ok: "정상",
    Status.degraded: "성능저하",
    Status.paused: "일시정지",
    Status.sending: "전송 중",
    Status.error: "오류",
}

#: SF Symbol names per state. macOS 11+.
SF_SYMBOLS: dict[Status, str] = {
    Status.starting: "clock",
    Status.ok: "bubble.left.and.bubble.right",
    Status.degraded: "exclamationmark.triangle",
    Status.paused: "pause.circle",
    Status.sending: "arrow.up.circle",
    Status.error: "xmark.octagon",
}


def select(candidates: Iterable[Status]) -> Status:
    """Highest-precedence state among *candidates*, or :attr:`Status.ok` if empty."""

    return min(candidates, key=_PRECEDENCE.__getitem__, default=Status.ok)


def symbol_name(state: Status) -> str:
    """The SF Symbol for *state*."""

    return SF_SYMBOLS[state]


def label(state: Status) -> str:
    """The Korean menu label for *state*."""

    return STATE_LABELS[state]


# ------------------------------------------------------------------ state input


def compute_state(
    *,
    kakaotalk_running: bool | None,
    permissions_ok: bool,
    polling: bool,
    paused: bool,
    sending: bool,
    consecutive_failures: int,
    last_sync_at: str | datetime | float | int | None,
    starting: bool,
    failure_threshold: int = FAILURE_THRESHOLD,
) -> Status:
    """Derive the menu bar state from plain, AppKit-free inputs.

    ``kakaotalk_running`` may be ``None`` when the probe did not answer; ``None``
    is "unknown" and never degrades the state. A sync that has never completed
    (``last_sync_at is None``) degrades, unless the backend is still ``starting``
    or is intentionally ``paused``.
    """

    candidates: set[Status] = set()
    if starting:
        candidates.add(Status.starting)
    if paused:
        candidates.add(Status.paused)
    if sending:
        candidates.add(Status.sending)
    if consecutive_failures >= failure_threshold:
        candidates.add(Status.error)
    if not starting and (
        not permissions_ok
        or kakaotalk_running is False
        or (not polling and not paused)
        or (last_sync_at is None and not paused)
        or (consecutive_failures > 0 and consecutive_failures < failure_threshold)
    ):
        candidates.add(Status.degraded)
    return select(candidates)


# --------------------------------------------------------------------- summary


def _coerce_datetime(value: str | datetime | float | int | None) -> datetime | None:
    """Parse an RFC3339 string, epoch seconds, or ``datetime`` into UTC-aware form."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coerce_now(now: datetime | float | int | str | None) -> datetime:
    resolved = _coerce_datetime(now)
    return resolved if resolved is not None else datetime.now(timezone.utc)


def relative_time(then: datetime, now: datetime) -> str:
    """A coarse Korean relative-time phrase (never absolute personal data)."""

    seconds = int(max(0.0, (now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}초 전"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}분 전"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 전"
    return f"{hours // 24}일 전"


def summary_text(
    state: Status,
    *,
    room_count: int | None = None,
    last_sync_at: str | datetime | float | int | None = None,
    now: datetime | float | int | str | None = None,
    consecutive_failures: int = 0,
) -> str:
    """One-line summary: state, counts, and relative timestamps only.

    Example: ``정상 · 방 42 · 최근 동기화 3초 전``. Passing *now* makes the relative
    phrase deterministic in tests.
    """

    parts = [STATE_LABELS[state]]
    if room_count is not None:
        parts.append(f"방 {room_count}")
    synced = _coerce_datetime(last_sync_at)
    if synced is not None:
        parts.append(f"최근 동기화 {relative_time(synced, _coerce_now(now))}")
    if consecutive_failures > 0:
        parts.append(f"연속 실패 {consecutive_failures}회")
    return " · ".join(parts)


# -------------------------------------------------------------------- snapshot


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything the menu bar needs for one render, with no personal data."""

    status: Status
    summary: str
    kakaotalk_running: bool | None = None
    permissions_ok: bool = True
    full_disk_access_ok: bool = True
    accessibility_trusted: bool | None = None
    polling: bool = True
    paused: bool = False
    autostart: bool = False
    webhooks: tuple[Mapping[str, object], ...] = field(default_factory=tuple)


def build_snapshot(
    *,
    kakaotalk_running: bool | None,
    permissions_ok: bool,
    polling: bool,
    paused: bool,
    sending: bool,
    consecutive_failures: int,
    last_sync_at: str | datetime | float | int | None,
    starting: bool,
    room_count: int | None = None,
    full_disk_access_ok: bool = True,
    accessibility_trusted: bool | None = None,
    autostart: bool = False,
    webhooks: Iterable[Mapping[str, object]] = (),
    now: datetime | float | int | str | None = None,
    failure_threshold: int = FAILURE_THRESHOLD,
) -> StatusSnapshot:
    """Compute a state plus its summary in one call."""

    state = compute_state(
        kakaotalk_running=kakaotalk_running,
        permissions_ok=permissions_ok,
        polling=polling,
        paused=paused,
        sending=sending,
        consecutive_failures=consecutive_failures,
        last_sync_at=last_sync_at,
        starting=starting,
        failure_threshold=failure_threshold,
    )
    summary = summary_text(
        state,
        room_count=room_count,
        last_sync_at=last_sync_at,
        now=now,
        consecutive_failures=consecutive_failures,
    )
    return StatusSnapshot(
        status=state,
        summary=summary,
        kakaotalk_running=kakaotalk_running,
        permissions_ok=permissions_ok,
        full_disk_access_ok=full_disk_access_ok,
        accessibility_trusted=accessibility_trusted,
        polling=polling,
        paused=paused,
        autostart=autostart,
        webhooks=tuple(webhooks),
    )


# ------------------------------------------------------------------ menu model

ACTION_OPEN_KAKAOTALK = "open_kakaotalk"
ACTION_OPEN_FULL_DISK_ACCESS = "open_full_disk_access"
ACTION_OPEN_ACCESSIBILITY = "open_accessibility"
ACTION_SYNC_NOW = "sync_now"
ACTION_TOGGLE_PAUSE = "toggle_pause"
ACTION_COPY_API_TOKEN = "copy_api_token"
ACTION_OPEN_LOG = "open_log"
ACTION_TOGGLE_AUTOSTART = "toggle_autostart"
ACTION_QUIT = "quit"

#: Item kinds: a disabled status line, a normal action, a checkbox, an
#: explanatory disabled line, or a separator.
KIND_STATUS = "status"
KIND_ACTION = "action"
KIND_CHECKBOX = "checkbox"
KIND_INFO = "info"
KIND_SEPARATOR = "separator"

#: Stable item ids, so tests and the view agree without matching titles.
ITEM_STATUS = "status_summary"
ITEM_KAKAOTALK = "kakaotalk"
ITEM_PERMISSIONS_FULL_DISK = "permissions_full_disk"
ITEM_PERMISSIONS_ACCESSIBILITY = "permissions_accessibility"
ITEM_PERMISSIONS_TARGET = "permissions_target"
ITEM_SYNC_NOW = "sync_now"
ITEM_TOGGLE_PAUSE = "toggle_pause"
ITEM_COPY_API_TOKEN = "copy_api_token"
ITEM_OPEN_LOG = "open_log"
ITEM_AUTOSTART = "autostart"
ITEM_WEBHOOK_STATUS = "webhook_status"
ITEM_INFO_UNSUPPORTED = "info_unsupported_types"
ITEM_INFO_STORAGE = "info_storage"
ITEM_QUIT = "quit"


@dataclass(frozen=True)
class MenuItem:
    """One menu entry, as data, so the menu is testable without AppKit."""

    item_id: str
    title: str
    enabled: bool
    action: str | None
    checked: bool | None = None
    kind: str = KIND_ACTION


def _running_label(running: bool | None) -> str:
    if running is None:
        return "알 수 없음"
    return "실행 중" if running else "꺼짐"


def _flag_label(ok: bool | None) -> str:
    if ok is None:
        return "알 수 없음"
    return "정상" if ok else "필요"


def _webhook_summary(
    webhooks: Iterable[Mapping[str, object]],
    now: datetime | float | int | str | None,
) -> str:
    hooks = list(webhooks)
    if not hooks:
        return "webhook: 구독 없음"
    active = [hook for hook in hooks if bool(hook.get("active"))]
    if not active:
        return f"webhook: 활성 0/{len(hooks)}개 · 최근 성공 없음"
    successes = [parsed for parsed in (_coerce_datetime(h.get("last_success_at")) for h in active) if parsed]
    if not successes:
        return f"webhook: 활성 {len(active)}/{len(hooks)}개 · 최근 성공 없음"
    reference = _coerce_now(now)
    return (
        f"webhook: 활성 {len(active)}/{len(hooks)}개 · "
        f"최근 성공 {relative_time(max(successes), reference)}"
    )


def menu_items(
    snapshot: StatusSnapshot,
    *,
    executable_path: str = "",
    now: datetime | float | int | str | None = None,
) -> list[MenuItem]:
    """The full menu as data (ADR-007), with no media/emoticon polling toggles.

    The first item is the disabled status summary; the login-autostart item is a
    checkbox whose state comes from ``snapshot.autostart``; the webhook item is
    disabled and read-only (active subscription count + last success, never a URL
    or a secret).
    """

    items: list[MenuItem] = [
        MenuItem(ITEM_STATUS, snapshot.summary, False, None, None, KIND_STATUS),
        MenuItem("separator-status", "", False, None, None, KIND_SEPARATOR),
        MenuItem(
            ITEM_KAKAOTALK,
            f"카카오톡: {_running_label(snapshot.kakaotalk_running)} · 열기",
            True,
            ACTION_OPEN_KAKAOTALK,
        ),
        MenuItem(
            ITEM_PERMISSIONS_FULL_DISK,
            f"권한 · 전체 디스크 접근: {_flag_label(snapshot.full_disk_access_ok)} · 설정 열기",
            True,
            ACTION_OPEN_FULL_DISK_ACCESS,
        ),
        MenuItem(
            ITEM_PERMISSIONS_ACCESSIBILITY,
            f"권한 · 손쉬운 사용: {_flag_label(snapshot.accessibility_trusted)} · 설정 열기",
            True,
            ACTION_OPEN_ACCESSIBILITY,
        ),
    ]
    if executable_path:
        items.append(
            MenuItem(
                ITEM_PERMISSIONS_TARGET,
                f"권한을 줄 실행 파일: {executable_path}",
                False,
                None,
                None,
                KIND_INFO,
            )
        )

    items.extend(
        [
            MenuItem("separator-actions", "", False, None, None, KIND_SEPARATOR),
            MenuItem(ITEM_SYNC_NOW, "지금 동기화", snapshot.polling, ACTION_SYNC_NOW),
            MenuItem(
                ITEM_TOGGLE_PAUSE,
                "재개" if snapshot.paused else "일시정지",
                True,
                ACTION_TOGGLE_PAUSE,
            ),
            MenuItem(ITEM_COPY_API_TOKEN, "API 토큰 복사", True, ACTION_COPY_API_TOKEN),
            MenuItem(ITEM_OPEN_LOG, "로그 열기", True, ACTION_OPEN_LOG),
            MenuItem(
                ITEM_AUTOSTART,
                "로그인 시 자동시작",
                True,
                ACTION_TOGGLE_AUTOSTART,
                snapshot.autostart,
                KIND_CHECKBOX,
            ),
            MenuItem("separator-webhook", "", False, None, None, KIND_SEPARATOR),
            MenuItem(
                ITEM_WEBHOOK_STATUS,
                _webhook_summary(snapshot.webhooks, now),
                False,
                None,
                None,
                KIND_STATUS,
            ),
            MenuItem("separator-info", "", False, None, None, KIND_SEPARATOR),
            MenuItem(
                ITEM_INFO_UNSUPPORTED,
                "지원하지 않는 메시지 타입은 고정 문구로 치환해 저장합니다",
                False,
                None,
                None,
                KIND_INFO,
            ),
            MenuItem(
                ITEM_INFO_STORAGE,
                "미디어·이모티콘은 다운로드·저장하지 않습니다 (저장공간·프라이버시)",
                False,
                None,
                None,
                KIND_INFO,
            ),
            MenuItem("separator-quit", "", False, None, None, KIND_SEPARATOR),
            MenuItem(ITEM_QUIT, "백엔드 종료", True, ACTION_QUIT),
        ]
    )
    return items
