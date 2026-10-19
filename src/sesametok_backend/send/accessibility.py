"""macOS 손쉬운 사용을 통해 실제 카카오톡 창을 구동한다.

이 모듈의 핵심은 *순서와 관찰*이다. 모든 단계는 UI를 관찰하거나 키 입력을
보내며, 값이 기록한 내용을 그대로 유지하지 않으면 파이프라인은 잘못된 대상에
동작하는 대신 멈춘다. 전송은 되돌릴 수 없으므로 기본값은 모호하면 거부하는 것이다.

상태: 순서, 게이트, 타이밍 상수, 오류 코드는 명세대로 구현되어 있다. 실제 검증에는
손쉬운 사용 권한(현재 백엔드를 실행하는 인터프리터에 허용되어 있음)과 운영자가
지정한 방이 필요하다. 그 테스트가 실행되기 전까지 이 파일은 미검증으로 간주한다.

이 모듈은 pyobjc를 지연 임포트하므로 나머지 백엔드(그리고 테스트 스위트)가 헤드리스
환경과 GUI 세션이 없는 머신에서도 동작한다.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .errors import SendError
from .targets import SendTarget, room_matches, search_query, validate_room_name

logger = logging.getLogger("sesametok_backend.send.accessibility")

# ---------------------------------------------------------------------- 상수

IDLE_GAP_SECONDS = 0.7
IDLE_MAX_SECONDS = 15.0
IDLE_POLL_SECONDS = 0.08
FRONTMOST_CHECKS = 25
FRONTMOST_INTERVAL = 0.04
FRONTMOST_RETRIES = 3
FRONTMOST_RETRY_DELAY = 0.06
WINDOW_APPEAR_WAIT = (20, 0.10)
OPEN_ATTEMPTS = 6
RECENT_ROWS = 60
MAIN_WINDOW_SAMPLE = 10
COMPOSE_DRAIN = (20, 0.05)
CONTENT_CONFIRM_WAIT = (30, 0.05)
RAISE_DELAY = 0.2
IMAGE_CONFIRM_WAIT = (40, 0.10)
DRAFT_VERIFY_WAIT = (30, 0.05)
TYPING_INTERVAL = 0.008
OVERLAY_ACK_MAX = 0.6
MAX_DESCENDANTS = 800
ROW_TITLE_DEPTH = 3
ROW_TITLE_LIMIT = 40
AX_TIMEOUT_SECONDS = 2.0
"""AX 호출 하나가 이 시간을 넘기면 오류로 돌아온다(무한 대기 방지)."""

SEND_DEADLINE_SECONDS = 60.0
"""방을 찾아 여는 단계의 전체 상한. 넘으면 추측하지 않고 명확한 오류로 끝낸다."""

KEY_ENTER = 36
KEY_CMD_V = 9
KEY_ESC = 53

IMAGE_UTIS = {
    ".png": "public.png",
    ".jpg": "public.jpeg",
    ".jpeg": "public.jpeg",
    ".gif": "com.compuserve.gif",
    ".tif": "public.tiff",
    ".tiff": "public.tiff",
    ".bmp": "com.microsoft.bmp",
    ".heic": "public.heic",
    ".webp": "org.webmproject.webp",
}

SEND_BUTTON_TITLES = ("Send", "전송")


class KakaoTalkNotRunning(SendError):
    def __init__(self) -> None:
        super().__init__("not_running")


class _Deadline:
    """발신 파이프라인 한 번의 전체 시간 상한."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self._end


@dataclass(frozen=True)
class WindowInfo:
    index: int
    title: str
    element: Any


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def _wait_for(predicate, attempts: int, interval: float):
    """*predicate*를 ``attempts × interval``초까지 폴링한다."""

    for _ in range(max(1, attempts)):
        value = predicate()
        if value:
            return value
        _sleep(interval)
    return None


def _load_frameworks() -> SimpleNamespace:
    """macOS 프레임워크를 임포트하거나, 권한 오류 코드와 함께 실패한다."""

    try:
        import objc  # noqa: F401
        from AppKit import (  # type: ignore[import-not-found]
            NSApplicationActivationPolicyAccessory,
            NSApplication,
            NSPasteboard,
            NSPasteboardTypeString,
            NSWorkspace,
        )
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXIsProcessTrusted,
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementPerformAction,
            AXUIElementSetAttributeValue,
            AXUIElementSetMessagingTimeout,
        )
        from Quartz import (  # type: ignore[import-not-found]
            CGEventCreateKeyboardEvent,
            CGEventCreateMouseEvent,
            CGEventPost,
            CGEventPostToPid,
            CGEventSetFlags,
            CGEventSetIntegerValueField,
            CGEventSourceSecondsSinceLastEventType,
            kCGEventFlagMaskCommand,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGMouseButtonLeft,
            kCGMouseEventClickState,
            kCGHIDEventTap,
            kCGEventSourceStateCombinedSessionState,
        )
    except Exception as exc:  # pragma: no cover - 기기에 따라 달라짐
        raise SendError(
            "accessibility_denied",
            f"macOS 손쉬운 사용 프레임워크를 사용할 수 없습니다: {exc}",
        ) from exc

    return SimpleNamespace(
        NSApplication=NSApplication,
        NSWorkspace=NSWorkspace,
        NSPasteboard=NSPasteboard,
        NSPasteboardTypeString=NSPasteboardTypeString,
        Accessory=NSApplicationActivationPolicyAccessory,
        AXIsProcessTrusted=AXIsProcessTrusted,
        CopyAttr=AXUIElementCopyAttributeValue,
        CreateApp=AXUIElementCreateApplication,
        PerformAction=AXUIElementPerformAction,
        SetAttr=AXUIElementSetAttributeValue,
        SetMessagingTimeout=AXUIElementSetMessagingTimeout,
        KeyEvent=CGEventCreateKeyboardEvent,
        KeyPost=CGEventPost,
        KeyPostToPid=CGEventPostToPid,
        MouseEvent=CGEventCreateMouseEvent,
        SetIntField=CGEventSetIntegerValueField,
        ClickState=kCGMouseEventClickState,
        LeftDown=kCGEventLeftMouseDown,
        LeftUp=kCGEventLeftMouseUp,
        LeftButton=kCGMouseButtonLeft,
        SetFlags=CGEventSetFlags,
        SecondsSinceLastEvent=CGEventSourceSecondsSinceLastEventType,
        CommandFlag=kCGEventFlagMaskCommand,
        HIDTap=kCGHIDEventTap,
        CombinedState=kCGEventSourceStateCombinedSessionState,
    )


def _attr(framework: SimpleNamespace, element: Any, name: str) -> Any:
    if element is None:
        return None
    try:
        error, value = framework.CopyAttr(element, name, None)
    except Exception:
        return None
    return value if error == 0 else None


def _text_attr(framework: SimpleNamespace, element: Any, name: str) -> str | None:
    value = _attr(framework, element, name)
    return value if isinstance(value, str) else None


_AX_VALUE_LIB: Any = None


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


def _ax_value_lib():
    """``AXValueGetValue``를 ctypes로 부른다.

    PyObjC의 바인딩은 인자 순서를 잘못 잡아 ``AXValueRef``를 정수로 해석하고,
    ``Quartz``에는 이 심볼이 아예 없다. 좌표를 읽지 못하면 행 클릭·전송 버튼 클릭이
    모두 불가능하므로 직접 부른다.
    """

    global _AX_VALUE_LIB
    if _AX_VALUE_LIB is None:
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        lib.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        lib.AXValueGetValue.restype = ctypes.c_bool
        _AX_VALUE_LIB = lib
    return _AX_VALUE_LIB


def _frame_of(framework: SimpleNamespace, element: Any) -> tuple[float, float, float, float] | None:
    """원소의 ``(x, y, width, height)``. 읽지 못하면 ``None``."""

    import objc

    position = _attr(framework, element, "AXPosition")
    size = _attr(framework, element, "AXSize")
    if position is None or size is None:
        return None
    if isinstance(position, (tuple, list)) and isinstance(size, (tuple, list)):
        try:
            return (
                float(position[0]),
                float(position[1]),
                float(size[0]),
                float(size[1]),
            )
        except (TypeError, ValueError, IndexError):
            return None
    point = _CGPoint()
    extent = _CGSize()
    lib = _ax_value_lib()
    try:
        ok_point = lib.AXValueGetValue(
            ctypes.c_void_p(objc.pyobjc_id(position)), 1, ctypes.byref(point)
        )
        ok_size = lib.AXValueGetValue(
            ctypes.c_void_p(objc.pyobjc_id(size)), 2, ctypes.byref(extent)
        )
    except Exception:
        return None
    if not (ok_point and ok_size):
        return None
    return (point.x, point.y, extent.width, extent.height)


def _element_center(framework: SimpleNamespace, element: Any) -> tuple[float, float] | None:
    """원소 프레임의 중심(화면 좌표). 읽지 못하면 ``None``."""

    frame = _frame_of(framework, element)
    if frame is None:
        return None
    x, y, width, height = frame
    return (x + width / 2.0, y + height / 2.0)


class KakaoTalkDriver:
    """단일 스레드로 동작하는 손쉬운 사용 드라이버."""

    def __init__(self, *, background_only: bool = False, curtain: Any = None) -> None:
        self.background_only = background_only
        self.framework = _load_frameworks()
        self.curtain = curtain
        self._pid: int | None = None
        self._deadline: _Deadline | None = None

    def _check_deadline(self, code: str, **details: object) -> None:
        if self._deadline is not None and self._deadline.expired():
            raise SendError(code, **details)

    # ------------------------------------------------------------- 사전 조건

    def _require_permission(self) -> None:
        if not bool(self.framework.AXIsProcessTrusted()):
            raise SendError("accessibility_denied")

    def _require_running(self) -> int:
        try:
            result = subprocess.run(
                ["pgrep", "-x", "KakaoTalk"], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SendError("not_running", f"pgrep을 실행할 수 없습니다: {exc}") from exc
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        if not pids:
            raise KakaoTalkNotRunning()
        self._pid = int(pids[0])
        return self._pid

    def _app_element(self):
        assert self._pid is not None
        app = self.framework.CreateApp(self._pid)
        try:
            # 응답이 없는 앱에 AX 호출을 걸면 무한히 기다릴 수 있다. 상한을 둔다.
            self.framework.SetMessagingTimeout(app, AX_TIMEOUT_SECONDS)
        except Exception:
            pass
        return app

    # ------------------------------------------------------------------- 창

    def _windows(self, app) -> list[WindowInfo]:
        raw = _attr(self.framework, app, "AXWindows") or []
        windows: list[WindowInfo] = []
        for index, element in enumerate(raw):
            title = _text_attr(self.framework, element, "AXTitle") or ""
            windows.append(WindowInfo(index=index, title=title, element=element))
        return windows

    def _matching_windows(self, app, wanted: str) -> list[WindowInfo]:
        return [window for window in self._windows(app) if room_matches(window.title, wanted)]

    def find_window(self, target: SendTarget):
        """*target*에 대해 창을 정확히 하나로 확정하거나 거부한다(추측하지 않는다)."""

        validate_room_name(target.room_name)
        app = self._app_element()
        matches = self._matching_windows(app, target.room_name)

        if len(matches) > 1:
            raise SendError("ambiguous_room", room=target.room_name, count=len(matches))
        if len(matches) == 1:
            return matches[0].element

        if self.background_only:
            raise SendError(
                "window_not_open",
                room=target.room_name,
                open=", ".join(window.title for window in self._windows(app)) or "(none)",
            )
        return self._open_room(app, target)

    def _named_rows(self, app, target: SendTarget) -> list[Any]:
        rows = self._rows(app)
        named: list[Any] = []
        for row in rows:
            title = self._row_title(row)
            if title and room_matches(title, target.room_name):
                named.append(row)
        logger.info("행 검색: rows=%d, matches=%d", len(rows), len(named))
        if len(named) > 1:
            raise SendError("ambiguous_room", room=target.room_name, count=len(named))
        return named

    def _open_by_clicking_row(self, app, target: SendTarget, *, to_pid: bool) -> bool:
        """채팅 목록의 행을 더블클릭해 방을 연다.

        ``to_pid=True``면 카카오톡에 직접 전달하므로 **활성화 없이** 열 수 있다.
        전역 클릭은 최전면 창에 떨어지므로 그 경로에서만 활성화한다.
        """

        named = self._named_rows(app, target)
        if not named:
            return False
        if not to_pid:
            self._activate(app)
            # 활성화하면 창이 움직일 수 있으므로 좌표를 그 뒤에 다시 읽는다.
            center = _element_center(self.framework, named[0])
        else:
            center = _element_center(self.framework, named[0])
        logger.info(
            "방 열기(클릭): to_pid=%s, 좌표=%s", to_pid, "ok" if center else "판독 실패"
        )
        if center is None:
            raise SendError("room_open_failed", room=target.room_name)
        self._double_click(*center, to_pid=to_pid)
        return True

    def _try_window(self, app, target: SendTarget, *, attempts: int, interval: float):
        """창이 나타나면 반환하고, 아니면 ``None``(예외를 던지지 않는다)."""

        for _ in range(attempts):
            matches = self._matching_windows(app, target.room_name)
            if len(matches) > 1:
                raise SendError("ambiguous_room", room=target.room_name, count=len(matches))
            if matches:
                return matches[0].element
            _sleep(interval)
        return None

    def _open_room(self, app, target: SendTarget):
        """방을 연다. **찾은 좌표를 재사용**해 두 클릭 방식을 연속 시도한다.

        목록은 메시지가 오면 즉시 재정렬되므로, 클릭 사이에 다시 찾으면 대상 행을
        놓치기 쉽다(실측: 두 번째 검색이 0건).
        """

        open_titles = ", ".join(w.title for w in self._windows(app))
        named = self._named_rows(app, target)
        center = _element_center(self.framework, named[0]) if named else None

        if center is not None:
            # 1) pid로만 클릭 — 앱을 앞으로 가져오지 않는다.
            self._double_click(*center, to_pid=True)
            window = self._try_window(app, target, attempts=20, interval=0.15)
            if window is not None:
                return window
            # 2) 활성화한 뒤 전역 더블클릭(행은 전역 클릭에만 반응한다).
            try:
                self._activate(app)
            except SendError as exc:
                logger.info("활성화 실패: %s", exc.code)
            moved = _element_center(self.framework, named[0]) or center
            self._double_click(*moved, to_pid=False)
            window = self._try_window(app, target, attempts=40, interval=0.15)
            if window is not None:
                return window
            logger.info("클릭으로 열지 못함: 창 수=%d", len(self._windows(app)))

        # 3) 폴백: 검색 → 목록
        last_error: SendError | None = None
        for attempt in range(OPEN_ATTEMPTS):
            self._check_deadline("room_open_failed", room=target.room_name, open=open_titles)
            try:
                if self._open_via_search(app, target):
                    return self._wait_for_single_window(app, target)
                if self._open_via_chat_list(app, target, recent_only=True):
                    return self._wait_for_single_window(app, target)
            except SendError as exc:
                logger.info("방 열기 폴백 %d 실패: %s", attempt, exc.code)
                if exc.code in ("ambiguous_room", "room_name_not_typable", "chat_list_moved"):
                    raise
                last_error = exc
            _sleep(0.1)

        if last_error is not None:
            raise last_error
        raise SendError("room_open_failed", room=target.room_name, open=open_titles)

    def _wait_for_single_window(self, app, target: SendTarget):
        def single():
            self._check_deadline("room_open_failed", room=target.room_name)
            matches = self._matching_windows(app, target.room_name)
            if len(matches) > 1:
                raise SendError("ambiguous_room", room=target.room_name, count=len(matches))
            return matches[0].element if matches else None

        window = _wait_for(single, *WINDOW_APPEAR_WAIT)
        if window is None:
            logger.info("창 대기 실패: 창 수=%d", len(self._windows(app)))
            raise SendError("room_open_failed", room=target.room_name)
        return window

    def _descendants(self, root, *, max_depth: int = 10, limit: int = MAX_DESCENDANTS) -> list[Any]:
        """깊이와 개수를 제한한 BFS.

        AX 호출은 프로세스 간 통신이라 비싸다. 깊이·개수 상한이 없으면 큰 창 하나를
        훑는 데 수 분이 걸리고, 그 사이 발신 요청은 끝나지 않는다(실측).
        """

        found: list[Any] = []
        stack: list[tuple[Any, int]] = [(root, 0)]
        while stack and len(found) < limit:
            if self._deadline is not None and self._deadline.expired():
                break
            element, depth = stack.pop()
            if depth >= max_depth:
                continue
            children = _attr(self.framework, element, "AXChildren") or []
            for child in children:
                found.append(child)
                stack.append((child, depth + 1))
        return found

    def _search_field(self, app):
        root = self._main_window_element(app) or app
        for element in self._descendants(root, max_depth=12, limit=1500):
            role = _text_attr(self.framework, element, "AXRole") or ""
            if role != "AXTextField":
                continue
            description = (
                _text_attr(self.framework, element, "AXPlaceholderValue")
                or _text_attr(self.framework, element, "AXDescription")
                or ""
            ).lower()
            if "search" in description or "검색" in description or description == "":
                return element
        return None

    def _open_via_search(self, app, target: SendTarget) -> bool:
        field = self._search_field(app)
        if field is None:
            raise SendError("search_unavailable")
        self._set_focus(field)
        query = search_query(target.room_name)
        self._type_text(query)
        _sleep(0.3)

        rows = self._rows(app)
        named = [row for row in rows if self._row_title(row) and room_matches(self._row_title(row), target.room_name)]
        if not named:
            return False
        if len(named) > 1 and len({self._row_title(row) for row in named}) > 1:
            raise SendError("ambiguous_room", room=target.room_name, count=len(named))
        self._press(named[0])
        return True

    def _open_via_chat_list(self, app, target: SendTarget, *, recent_only: bool) -> bool:
        """목록에서 이름으로 행을 찾아 **더블클릭**한다.

        행은 ``AXPress``에 반응하지 않으므로 좌표 클릭이 필요하다. 위치(index)로
        재확인하면 다른 창의 행이 섞여 순서가 흔들리므로, 클릭 직전에 **이름으로**
        다시 찾는다.
        """

        named = self._named_rows(app, target)
        if not named:
            return False
        self._activate(app)
        current = self._named_rows(app, target)
        if not current:
            raise SendError("chat_list_moved", room=target.room_name)
        center = _element_center(self.framework, current[0])
        if center is None:
            raise SendError("room_open_failed", room=target.room_name)
        self._double_click(*center, to_pid=False)
        return True

    def _main_window_element(self, app):
        """가장 큰 창을 메인 창으로 본다(채팅 목록이 있는 창)."""

        best = None
        best_area = -1.0
        for window in self._windows(app):
            frame = _frame_of(self.framework, window.element)
            if frame is None:
                if best is None:
                    best = window.element
                continue
            area = abs(frame[2] * frame[3])
            if area > best_area:
                best, best_area = window.element, area
        return best

    def _rows(self, app) -> list[Any]:
        """메인 창 안의 목록 행들.

        앱 전체를 훑으면 창이 바뀔 때 순회 예산이 밀려 목록을 놓친다(실측).
        """

        root = self._main_window_element(app) or app
        rows: list[Any] = []
        for element in self._descendants(root, max_depth=12, limit=1500):
            role = _text_attr(self.framework, element, "AXRole") or ""
            if role in ("AXRow", "AXOutlineRow"):
                rows.append(element)
        return rows

    def _row_title(self, row) -> str | None:
        """행 하나의 제목. 행마다 트리 전체를 훑지 않도록 얕게 본다."""

        for element in [row, *self._descendants(row, max_depth=ROW_TITLE_DEPTH, limit=ROW_TITLE_LIMIT)]:
            role = _text_attr(self.framework, element, "AXRole") or ""
            if role in ("AXStaticText", "AXCell", "AXTextField"):
                value = _text_attr(self.framework, element, "AXValue") or _text_attr(
                    self.framework, element, "AXTitle"
                )
                if value:
                    return value
        return None

    # ---------------------------------------------------------------- 요소

    def _find_compose_box(self, window):
        """창 안의 ``AXScrollArea > AXTextArea``."""

        parent_roles: dict[int, str] = {}
        text_areas: list[Any] = []
        stack = [window]
        seen = 0
        while stack and seen < MAX_DESCENDANTS:
            element = stack.pop()
            role = _text_attr(self.framework, element, "AXRole") or ""
            for child in _attr(self.framework, element, "AXChildren") or []:
                seen += 1
                parent_roles[id(child)] = role
                if (_text_attr(self.framework, child, "AXRole") or "") == "AXTextArea":
                    text_areas.append(child)
                stack.append(child)

        for element in text_areas:
            if parent_roles.get(id(element)) in ("AXScrollArea", ""):
                return element
        return None

    def _find_button(self, window, titles: tuple[str, ...]):
        for element in self._descendants(window):
            role = _text_attr(self.framework, element, "AXRole") or ""
            if role != "AXButton":
                continue
            title = _text_attr(self.framework, element, "AXTitle") or ""
            if title in titles:
                return element
        return None

    def _value(self, element) -> str | None:
        value = _attr(self.framework, element, "AXValue")
        return value if isinstance(value, str) else None

    def _set_value(self, element, text: str) -> None:
        error = self.framework.SetAttr(element, "AXValue", text)
        if error != 0:
            raise SendError("set_value_failed", n=error)

    def _set_focus(self, element) -> None:
        self.framework.SetAttr(element, "AXFocused", True)

    def _press(self, element) -> None:
        self.framework.PerformAction(element, "AXPress")

    def _require_pid(self) -> int:
        if self._pid is None:
            from ..kakao import store

            self._pid = store.kakaotalk_pid()
        if self._pid is None:
            raise SendError("kakaotalk_not_running")
        return self._pid

    def _double_click(self, x: float, y: float, *, to_pid: bool) -> None:
        """더블클릭을 보낸다.

        ``to_pid=True``면 카카오톡에 **직접** 전달되므로 앱을 앞으로 가져오지 않아도
        된다(전역 포스트는 최전면 창에 떨어지므로 활성화가 필요하다).
        """

        pid = self._require_pid() if to_pid else None
        for click in (1, 2):
            for kind in (self.framework.LeftDown, self.framework.LeftUp):
                event = self.framework.MouseEvent(None, kind, (x, y), self.framework.LeftButton)
                self.framework.SetIntField(event, self.framework.ClickState, click)
                if pid is None:
                    self.framework.KeyPost(self.framework.HIDTap, event)
                else:
                    self.framework.KeyPostToPid(pid, event)
            _sleep(0.04)

    def _post_key_to_pid(self, keycode: int, *, flags: int = 0) -> None:
        """키 이벤트를 카카오톡에만 전달한다(포커스·활성화 없음)."""

        pid = self._require_pid()
        for down in (True, False):
            event = self.framework.KeyEvent(None, keycode, down)
            if flags:
                self.framework.SetFlags(event, flags)
            self.framework.KeyPostToPid(pid, event)

    def _raise(self, element) -> None:
        self.framework.PerformAction(element, "AXRaise")

    # ---------------------------------------------------------------- 입력

    def _post_key(self, keycode: int, *, flags: int = 0) -> None:
        down = self.framework.KeyEvent(None, keycode, True)
        up = self.framework.KeyEvent(None, keycode, False)
        if flags:
            self.framework.SetFlags(down, flags)
            self.framework.SetFlags(up, flags)
        self.framework.KeyPost(self.framework.HIDTap, down)
        self.framework.KeyPost(self.framework.HIDTap, up)

    def _type_text(self, text: str) -> None:
        for character in text:
            self._post_unicode(character)
            _sleep(TYPING_INTERVAL)

    def _post_unicode(self, character: str) -> None:
        from Quartz import CGEventKeyboardSetUnicodeString

        down = self.framework.KeyEvent(None, 0, True)
        up = self.framework.KeyEvent(None, 0, False)
        CGEventKeyboardSetUnicodeString(down, len(character), character)
        CGEventKeyboardSetUnicodeString(up, len(character), character)
        self.framework.KeyPost(self.framework.HIDTap, down)
        self.framework.KeyPost(self.framework.HIDTap, up)

    def _seconds_since_last_input(self) -> float:
        return float(
            self.framework.SecondsSinceLastEvent(self.framework.CombinedState, 0xFFFFFFFF)
        )

    # ------------------------------------------------------------- 포커스 정책

    def _wait_for_idle(self, *, immediate: bool = False) -> None:
        if immediate:
            return
        deadline = time.monotonic() + IDLE_MAX_SECONDS
        while time.monotonic() < deadline:
            if self._seconds_since_last_input() >= IDLE_GAP_SECONDS:
                return
            _sleep(IDLE_POLL_SECONDS)
        raise SendError("user_busy", n=int(IDLE_MAX_SECONDS))

    def _activate(self, app) -> None:
        """카카오톡을 앞으로 가져오고, 최전면에 머물렀는지 확인한다.

        ``AXRaise``만으로는 다른 앱이 최전면일 때 활성화되지 않는다(실측: focus_lost).
        ``NSRunningApplication``으로 활성화를 요청해야 한다.
        """

        try:
            from AppKit import (  # type: ignore[import-not-found]
                NSApplicationActivateIgnoringOtherApps,
                NSRunningApplication,
            )

            running = NSRunningApplication.runningApplicationWithProcessIdentifier_(
                self._require_pid()
            )
            if running is not None:
                running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        except Exception as exc:  # 프레임워크가 없어도 AXRaise는 시도한다
            logger.warning("앱 활성화 요청 실패(%s)", type(exc).__name__)
        self.framework.NSWorkspace.sharedWorkspace()
        self.framework.PerformAction(app, "AXRaise")
        for _ in range(FRONTMOST_RETRIES):
            _sleep(FRONTMOST_RETRY_DELAY)
            if self._frontmost(app):
                return
        raise SendError("focus_lost")

    def _frontmost(self, app) -> bool:
        def check():
            return bool(_attr(self.framework, app, "AXFrontmost"))

        return bool(_wait_for(check, FRONTMOST_CHECKS, FRONTMOST_INTERVAL))

    # ------------------------------------------------------------ 공개 API

    def send_text(
        self,
        target: SendTarget,
        text: str,
        *,
        mode: str = "send",
        background_only: bool = False,
    ):
        from ..core.send_queue import SendOutcome

        self.background_only = background_only
        validate_room_name(target.room_name)
        self._require_permission()
        self._require_running()
        self._deadline = _Deadline(SEND_DEADLINE_SECONDS)
        app = self._app_element()

        window = self.find_window(target)
        compose = self._find_compose_box(window)
        if compose is None:
            raise SendError("compose_box_not_found", room=target.room_name)

        if background_only:
            current = self._value(compose)
            if current is None:
                raise SendError("background_compose_unreadable", room=target.room_name)
            if current != "":
                raise SendError("background_compose_not_empty", room=target.room_name)

        if mode == "draft":
            self._stage_draft(window, compose, target, text)
            return SendOutcome("drafted")

        self._set_focus(compose)
        self._set_value(compose, text)
        if self._value(compose) != text:
            raise SendError("set_value_failed", n=0)

        if background_only:
            if self._value(compose) != text:
                raise SendError("background_compose_changed", room=target.room_name)
            self._post_key_to_pid(KEY_ENTER)
            if self._compose_emptied(compose):
                return SendOutcome("sent")
            return SendOutcome(
                "unconfirmed",
                "background_send_not_accepted",
                str(SendError("background_send_not_accepted")),
            )

        # 먼저 포커스를 뺏지 않는 경로를 시도한다(전송 버튼 AXPress / pid Enter).
        if self._send_without_focus(window, compose, text):
            return SendOutcome("sent")
        # 마지막 수단: 잠깐 최전면으로 올려 전역 Enter/클릭(커튼 포함).
        if self._send_visible(self._app_element(), window, compose, text):
            return SendOutcome("sent")
        return SendOutcome("unconfirmed", "not_sent", str(SendError("not_sent")))

    def _send_without_focus(self, window, compose, text: str) -> bool:
        """포커스·활성화 없이 전송을 시도한다.

        1) 전송 버튼 ``AXPress`` — 좌표 클릭도 키보드도 필요 없다.
        2) 카카오톡 pid로만 Enter 전달 — 최전면이 아니어도 도달한다.

        최신 빌드는 ``AXValue`` 쓰기만으로는 전송되지 않으므로 이 두 경로가 필요하다.
        """

        button = self._find_button(window, SEND_BUTTON_TITLES)
        if button is not None:
            try:
                self._press(button)
            except Exception:  # 버튼이 눌리지 않아도 다음 경로가 있다
                pass
            if self._compose_emptied(compose):
                return True
        try:
            self._post_key_to_pid(KEY_ENTER)
        except SendError:
            return False
        return self._compose_emptied(compose)

    def _compose_emptied(self, compose) -> bool:
        return _wait_for(lambda: self._value(compose) == "", *COMPOSE_DRAIN) is not None

    def _send_visible(self, app, window, compose, text: str) -> bool:
        """14~17단계: 입력이 멈추기를 기다린 뒤 화면을 가져가고 Enter / 전송 클릭.

        입력 커튼을 사용할 수 있으면 먼저 올린다. 커튼을 표시할 수 없으면
        **차단 없이** 계속 진행하되(문서화된 폴백) 모든 최전면 검사는 그대로
        유지한다. 보이지 않게 차단하는 것은 결코 허용되지 않는다.
        """

        shown = self._raise_curtain()
        try:
            self._wait_for_idle()
            if self._curtain_cancelled():
                raise SendError("cancelled")
            self._activate(app)          # 14단계
            self._raise(window)          # 15단계
            _sleep(RAISE_DELAY)
            self._set_focus(compose)
            if self._value(compose) != text:
                raise SendError("value_mismatch", what="입력 상자")

            self._post_key(KEY_ENTER)    # 16단계
            if self._compose_emptied(compose):
                return True

            button = self._find_button(window, SEND_BUTTON_TITLES)
            if button is not None:       # 17단계
                center = _element_center(self.framework, button)
                if center is not None:
                    self._click(*center)
                    if self._compose_emptied(compose):
                        return True
            return False
        finally:
            if shown:
                self.curtain.hide()

    def _raise_curtain(self) -> bool:
        if self.curtain is None:
            return False
        try:
            shown = bool(self.curtain.show())
        except Exception as exc:
            logger.warning("커튼을 표시하지 못했습니다(%s). 차단 없이 계속합니다", type(exc).__name__)
            return False
        if not shown:
            logger.warning("커튼 표시가 확인되지 않았습니다. 차단 없이 계속합니다")
        return shown

    def _curtain_cancelled(self) -> bool:
        return bool(self.curtain is not None and getattr(self.curtain, "cancelled", False))

    def _click(self, x: float, y: float) -> None:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost as post,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGMouseButtonLeft,
        )

        for event_type in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
            event = CGEventCreateMouseEvent(None, event_type, (x, y), kCGMouseButtonLeft)
            post(self.framework.HIDTap, event)

    def _stage_draft(self, window, compose, target: SendTarget, text: str) -> None:
        """임시 내용 작성에는 절대 ``AXValue``를 쓰면 안 된다: 쓰는 것 자체가 전송일 수 있다."""

        if self.background_only:
            # 임시 내용 작성은 최전면 앱에 붙여넣는 것이므로 백그라운드 전용
            # 모드에서는 금지된다.
            raise SendError("background_only_unsupported")
        if self._value(compose) not in ("", None):
            raise SendError("background_compose_not_empty", room=target.room_name)
        self._paste_text(text)
        staged = _wait_for(lambda: self._value(compose) == text, *DRAFT_VERIFY_WAIT)
        if staged is None:
            raise SendError("draft_not_staged", room=target.room_name)

    def _paste_text(self, text: str) -> None:
        pasteboard = self.framework.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(text, self.framework.NSPasteboardTypeString)
        # 최전면을 다시 확인하지 않고 Cmd+V를 보내는 일은 없다.
        self._wait_for_idle()
        app = self._app_element()
        if not self._frontmost(app):
            self._activate(app)
        self._post_key(KEY_CMD_V, flags=self.framework.CommandFlag)

    def send_image(self, target: SendTarget, path: Path, *, background_only: bool = False):
        from ..core.send_queue import SendOutcome

        self.background_only = background_only
        extension = path.suffix.lower()
        uti = IMAGE_UTIS.get(extension)
        if uti is None or not path.is_file():
            raise SendError(
                "bad_request", f"지원하지 않는 이미지 형식입니다 {extension!r}"
            )
        self._require_permission()
        self._require_running()
        self._deadline = _Deadline(SEND_DEADLINE_SECONDS)

        app = self._app_element()
        window = self.find_window(target)
        compose = self._find_compose_box(window)
        if compose is None:
            raise SendError("compose_box_not_found", room=target.room_name)

        pasteboard = self.framework.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        from AppKit import NSImage  # type: ignore[import-not-found]

        image = NSImage.alloc().initWithContentsOfFile_(str(path))
        if image is None:
            raise SendError("bad_request", "이미지 파일을 읽을 수 없습니다")
        pasteboard.writeObjects_([image])

        if background_only:
            # 이미지 붙여넣기는 최전면 앱으로 전역 Cmd+V를 보내야 한다.
            raise SendError("background_only_unsupported")

        self._wait_for_idle()
        shown = self._raise_curtain()
        try:
            if self._curtain_cancelled():
                raise SendError("cancelled")
            self._activate(app)
            self._raise(window)
            _sleep(RAISE_DELAY)
            self._post_key(KEY_CMD_V, flags=self.framework.CommandFlag)

            if self._confirm_image_send(window):
                return SendOutcome("sent")
            return SendOutcome(
                "unconfirmed", "image_not_sent", str(SendError("image_not_sent"))
            )
        finally:
            if shown:
                self.curtain.hide()

    def _confirm_image_send(self, window) -> bool:
        from ..core.send_queue import SendOutcome  # noqa: F401 - 일관성을 위해

        def confirmed() -> bool:
            sheet = self._find_role(window, "AXSheet", depth_limit=4)
            if sheet is not None:
                for element in [sheet, *self._descendants(sheet)]:
                    role = _text_attr(self.framework, element, "AXRole") or ""
                    title = _text_attr(self.framework, element, "AXTitle") or ""
                    if role == "AXButton" and title in ("전송", "Send", "확인", "OK"):
                        self._press(element)
                        return True
            return False

        return _wait_for(confirmed, *IMAGE_CONFIRM_WAIT) is not None

    def _find_role(self, root, role_name: str, *, depth_limit: int):
        stack = [(root, 0)]
        while stack:
            element, depth = stack.pop()
            if depth > depth_limit:
                continue
            for child in _attr(self.framework, element, "AXChildren") or []:
                if (_text_attr(self.framework, child, "AXRole") or "") == role_name:
                    return child
                stack.append((child, depth + 1))
        return None

    def cancel(self) -> None:
        """진행 중인 보이는 전송을 취소한다(Esc는 화면에 표시된 뒤에만 유효하다)."""

        self._post_key(KEY_ESC)
