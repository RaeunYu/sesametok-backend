"""메뉴바 상태 모델: 상태, 심볼, 요약, 메뉴 내용만 담은 순수 로직.

이 모듈은 의도적으로 **AppKit에 의존하지 않는다**. 그래서 헤드리스로 임포트하고
단위 테스트할 수 있다. ADR-007에 따라 수정된 상태 모델과 메뉴 내용을 담고 있다:
"미디어 폴링 여부" / "이모티콘 폴링 여부" 토글은 **제거**되었고, 읽기 전용
webhook 상태 항목이 **추가**되었다.

프라이버시 규칙: 여기서 만들어지는 모든 문자열은 상태, 개수, 타임스탬프만으로
구성된다. 방 이름, 닉네임, 메시지 본문은 메뉴 텍스트, 툴팁, 로그에 절대 들어가지
않는다.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core.preferences import DEFAULT_POLL_SECONDS, coerce_poll_seconds

#: 이 값 이상으로 연속 폴링 실패가 쌓이면 ``Status.error``가 된다.
#: 1 이상 이 임계값 미만은 ``Status.degraded``다.
FAILURE_THRESHOLD = 3


class Status(enum.Enum):
    """메뉴바 상태. 우선순위가 높은 것부터 낮은 것 순으로 선언한다.

    우선순위: ``error`` > ``degraded`` > ``sending`` >
    ``paused`` > ``starting`` > ``ok``.
    """

    error = "error"
    degraded = "degraded"
    sending = "sending"
    paused = "paused"
    starting = "starting"
    ok = "ok"


_PRECEDENCE: dict[Status, int] = {state: rank for rank, state in enumerate(Status)}

#: 메뉴에 표시할 사람이 읽는 라벨.
STATE_LABELS: dict[Status, str] = {
    Status.starting: "기동 중",
    Status.ok: "정상",
    Status.degraded: "성능저하",
    Status.paused: "일시정지",
    Status.sending: "전송 중",
    Status.error: "오류",
}

#: 상태별 SF Symbol 이름. macOS 11 이상.
SF_SYMBOLS: dict[Status, str] = {
    Status.starting: "clock",
    Status.ok: "bubble.left.and.bubble.right",
    Status.degraded: "exclamationmark.triangle",
    Status.paused: "pause.circle",
    Status.sending: "arrow.up.circle",
    Status.error: "xmark.octagon",
}


def select(candidates: Iterable[Status]) -> Status:
    """*candidates* 중 우선순위가 가장 높은 상태. 비어 있으면 :attr:`Status.ok`."""

    return min(candidates, key=_PRECEDENCE.__getitem__, default=Status.ok)


def symbol_name(state: Status) -> str:
    """*state*에 해당하는 SF Symbol."""

    return SF_SYMBOLS[state]


def label(state: Status) -> str:
    """*state*에 해당하는 한국어 메뉴 라벨."""

    return STATE_LABELS[state]


# ------------------------------------------------------------------ 상태 입력


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
    """AppKit에 의존하지 않는 순수 입력으로부터 메뉴바 상태를 도출한다.

    ``kakaotalk_running``은 프로브가 응답하지 않았을 때 ``None``일 수 있다.
    ``None``은 "알 수 없음"이며 상태를 결코 나쁘게 만들지 않는다. 한 번도 완료되지
    않은 동기화(``last_sync_at is None``)는 백엔드가 아직 ``starting``이거나
    의도적으로 ``paused``인 경우가 아니면 성능저하로 본다.
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


# --------------------------------------------------------------------- 요약


def _coerce_datetime(value: str | datetime | float | int | None) -> datetime | None:
    """RFC3339 문자열, epoch 초, 또는 ``datetime``을 UTC 기준 aware 형태로 파싱한다."""

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
    """대략적인 한국어 상대 시간 표현(절대적인 개인 정보는 결코 담지 않는다)."""

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
    """한 줄 요약: 상태, 개수, 상대 시간만 담는다.

    예: ``정상 · 방 42 · 최근 동기화 3초 전``. *now*를 넘기면 테스트에서 상대
    시간 표현이 결정적이 된다.
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


# -------------------------------------------------------------------- 스냅샷


@dataclass(frozen=True)
class StatusSnapshot:
    """메뉴바가 한 번 그리는 데 필요한 모든 것. 개인 데이터는 없다."""

    status: Status
    summary: str
    kakaotalk_running: bool | None = None
    permissions_ok: bool = True
    store_readable: bool = True
    accessibility_trusted: bool | None = None
    polling: bool = True
    paused: bool = False
    autostart: bool = False
    managed: bool = False
    """launchd가 이 프로세스를 관리하면 True이며, 그러면 메뉴의 재시작 항목이 활성화된다."""

    poll_seconds: float = DEFAULT_POLL_SECONDS
    """현재 폴링 주기(초)이며, "동기화 주기" 하위 메뉴의 체크 표시를 정한다."""

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
    store_readable: bool = True,
    accessibility_trusted: bool | None = None,
    autostart: bool = False,
    managed: bool = False,
    poll_seconds: float | None = None,
    webhooks: Iterable[Mapping[str, object]] = (),
    now: datetime | float | int | str | None = None,
    failure_threshold: int = FAILURE_THRESHOLD,
) -> StatusSnapshot:
    """상태와 그 요약을 한 번에 계산한다."""

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
    interval = coerce_poll_seconds(poll_seconds)
    return StatusSnapshot(
        status=state,
        summary=summary,
        kakaotalk_running=kakaotalk_running,
        permissions_ok=permissions_ok,
        store_readable=store_readable,
        accessibility_trusted=accessibility_trusted,
        polling=polling,
        paused=paused,
        autostart=autostart,
        managed=managed,
        poll_seconds=interval if interval is not None else DEFAULT_POLL_SECONDS,
        webhooks=tuple(webhooks),
    )


# ------------------------------------------------------------------ 메뉴 모델

ACTION_OPEN_KAKAOTALK = "open_kakaotalk"
ACTION_OPEN_FULL_DISK_ACCESS = "open_full_disk_access"
ACTION_OPEN_ACCESSIBILITY = "open_accessibility"
ACTION_SYNC_NOW = "sync_now"
ACTION_TOGGLE_PAUSE = "toggle_pause"
ACTION_COPY_API_TOKEN = "copy_api_token"
ACTION_OPEN_LOG = "open_log"
ACTION_TOGGLE_AUTOSTART = "toggle_autostart"
ACTION_QUIT = "quit"
ACTION_RESTART = "restart"
ACTION_SET_INTERVAL = "set_interval"

#: 항목 종류: 비활성 상태 줄, 일반 동작, 체크박스, 설명용 비활성 줄, 구분선,
#: 폴링 주기 선택지(라디오형, 현재 값에만 체크).
KIND_STATUS = "status"
KIND_ACTION = "action"
KIND_CHECKBOX = "checkbox"
KIND_INFO = "info"
KIND_SEPARATOR = "separator"
KIND_INTERVAL = "interval"

#: 제목을 맞춰보지 않고도 테스트와 뷰가 합의할 수 있도록 안정적인 항목 id를 쓴다.
ITEM_STATUS = "status_summary"
ITEM_KAKAOTALK = "kakaotalk"
ITEM_PERMISSIONS_FULL_DISK = "permissions_full_disk"
ITEM_PERMISSIONS_ACCESSIBILITY = "permissions_accessibility"
ITEM_PERMISSIONS_TARGET = "permissions_target"
ITEM_PERMISSIONS_FULL_DISK_SETTINGS = "permissions_full_disk_settings"
ITEM_PERMISSIONS_ACCESSIBILITY_SETTINGS = "permissions_accessibility_settings"
ITEM_SYNC_NOW = "sync_now"
ITEM_TOGGLE_PAUSE = "toggle_pause"
ITEM_COPY_API_TOKEN = "copy_api_token"
ITEM_OPEN_LOG = "open_log"
ITEM_AUTOSTART = "autostart"
ITEM_WEBHOOK_STATUS = "webhook_status"
ITEM_INFO_UNSUPPORTED = "info_unsupported_types"
ITEM_INFO_STORAGE = "info_storage"
ITEM_RESTART = "restart"
ITEM_QUIT = "quit"
ITEM_INTERVAL = "interval"

#: 하위 메뉴 "동기화 주기"의 선택지(초). 저장값이 이 밖의 값이면 어느 항목도
#: 체크하지 않는다 — 현재 값을 다른 값으로 왜곡해서 보여주지 않는다.
INTERVAL_CHOICES: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0, 60.0)

#: 주기 하위 메뉴의 제목.
INTERVAL_MENU_TITLE = "동기화 주기"


def interval_label(seconds: float) -> str:
    """주기 선택지의 한국어 라벨(예: ``2초마다``)."""

    return f"{seconds:g}초마다"


def interval_item_id(seconds: float) -> str:
    """주기 선택지의 안정적인 항목 id(예: ``interval_10``)."""

    return f"{ITEM_INTERVAL}_{seconds:g}"


@dataclass(frozen=True)
class MenuItem:
    """AppKit 없이도 테스트할 수 있도록 메뉴 항목 하나를 데이터로 표현한다.

    *value*는 항목이 표현하는 값(폴링 주기 선택지의 초)이고, *children*은 하위
    메뉴 항목들이다. 둘 다 없으면 지금까지의 평범한 항목과 같다.
    """

    item_id: str
    title: str
    enabled: bool
    action: str | None
    checked: bool | None = None
    kind: str = KIND_ACTION
    value: float | None = None
    children: tuple["MenuItem", ...] = ()


def interval_menu_items(current: float | None) -> list[MenuItem]:
    """주기 선택지 항목들. *current*와 정확히 같은 값에만 체크를 단다.

    비정상이거나 선택지에 없는 *current*는 체크 없이 넘어가며, 어떤 라벨도
    바꾸지 않는다(현재 값을 다른 값으로 왜곡해서 보여주지 않는다).
    """

    selected = coerce_poll_seconds(current)
    return [
        MenuItem(
            interval_item_id(seconds),
            interval_label(seconds),
            True,
            ACTION_SET_INTERVAL,
            selected == seconds,
            KIND_INTERVAL,
            value=seconds,
        )
        for seconds in INTERVAL_CHOICES
    ]


def _running_label(running: bool | None) -> str:
    if running is None:
        return "알 수 없음"
    return "실행 중" if running else "꺼짐"


def _read_label(ok: bool) -> str:
    """읽기 가능 여부 라벨(권한 유무가 아니라 실제 접근 가능성)."""

    return "정상" if ok else "권한 필요"


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
    """전체 메뉴를 데이터로 표현한다(ADR-007). 미디어/이모티콘 폴링 토글은 없다.

    첫 항목은 비활성 상태 요약이다. 로그인 자동시작 항목은 체크박스이며 상태는
    ``snapshot.autostart``에서 온다. webhook 항목은 비활성 읽기 전용이다(활성 구독
    개수 + 최근 성공 시각이며, URL이나 비밀 값은 결코 넣지 않는다).

    표기는 짧게 유지한다: 권한 상태와 "설정 열기"는 서로 다른 항목으로 나뉘고, 권한을
    줄 실행 파일 경로 같은 긴 안내는 메뉴에 넣지 않는다(그 정보는 ``status`` 커맨드와
    로그가 담당한다). ``executable_path`` 인자는 호환성을 위해 남겨 두지만 더 이상
    메뉴에 그리지 않는다.

    재시작 항목은 launchd가 관리하는 프로세스(``snapshot.managed``)에서만 활성화되며,
    아니면 이유를 제목에 담아 비활성으로 둔다.

    "동기화 주기" 하위 메뉴는 폴링 주기 선택지(2/5/10/30/60초)를 담고, 현재 값에만
    체크 표시를 단다. 선택은 :data:`ACTION_SET_INTERVAL`로 전달되며, 선택지 값(초)은
    항목의 :attr:`MenuItem.value`에 있다.
    """

    items: list[MenuItem] = [
        MenuItem(ITEM_STATUS, snapshot.summary, False, None, None, KIND_STATUS),
        MenuItem("separator-status", "", False, None, None, KIND_SEPARATOR),
        MenuItem(
            ITEM_KAKAOTALK,
            f"카카오톡: {_running_label(snapshot.kakaotalk_running)}",
            True,
            ACTION_OPEN_KAKAOTALK,
        ),
        MenuItem(
            ITEM_PERMISSIONS_FULL_DISK,
            f"카카오톡 읽기: {_read_label(snapshot.store_readable)}",
            False,
            None,
            None,
            KIND_INFO,
        ),
        MenuItem(
            ITEM_PERMISSIONS_ACCESSIBILITY,
            f"손쉬운 사용: {_flag_label(snapshot.accessibility_trusted)}",
            False,
            None,
            None,
            KIND_INFO,
        ),
        MenuItem(
            ITEM_PERMISSIONS_FULL_DISK_SETTINGS,
            "전체 디스크 접근 설정 열기",
            True,
            ACTION_OPEN_FULL_DISK_ACCESS,
        ),
        MenuItem(
            ITEM_PERMISSIONS_ACCESSIBILITY_SETTINGS,
            "손쉬운 사용 설정 열기",
            True,
            ACTION_OPEN_ACCESSIBILITY,
        ),
    ]

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
            MenuItem(
                ITEM_INTERVAL,
                INTERVAL_MENU_TITLE,
                True,
                None,
                None,
                KIND_ACTION,
                children=tuple(interval_menu_items(snapshot.poll_seconds)),
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
                "지원하지 않는 메시지 타입: 고정 문구 치환",
                False,
                None,
                None,
                KIND_INFO,
            ),
            MenuItem(
                ITEM_INFO_STORAGE,
                "미디어·이모티콘: 다운로드·저장하지 않음",
                False,
                None,
                None,
                KIND_INFO,
            ),
            MenuItem("separator-quit", "", False, None, None, KIND_SEPARATOR),
            MenuItem(
                ITEM_RESTART,
                "재시작" if snapshot.managed else "재시작 · launchd로 실행 중일 때만",
                snapshot.managed,
                ACTION_RESTART,
            ),
            MenuItem(ITEM_QUIT, "종료", True, ACTION_QUIT),
        ]
    )
    return items
