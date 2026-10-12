"""입력 차단 커튼.

이미지 붙여넣기나 실제 더블클릭을 위해 카카오톡을 앞으로 가져오면, 사용자의 다음
키 입력이 엉뚱한 애플리케이션에 들어갈 수 있다. 커튼이 올라와 있는 동안 세션 수준
``CGEventTap``이 사용자 입력을 삼키고, 이 프로세스가 직접 합성한 이벤트
(``kCGEventSourceUserData``에 태깅됨)만 통과시킨다.

세 가지 규칙이 핵심을 지탱한다:

* **커튼은 결코 보이지 않는 상태가 되지 않는다.** 삼켜진 키는 큐에 쌓이지 않고
  버려지므로, 조용히 입력을 차단하는 것은 안전하지 않다. 커튼을 표시할 수 없으면
  ``show()``가 ``False``를 반환하며, 호출자는 보이는 경로로 진행해서는 안 된다.
* **창이 key가 되어서는 안 된다.** 우리 프로세스가 활성화되면 카카오톡이 최전면
  자리를 잃고 붙여넣기가 실패한다.
* **모든 AppKit 작업은 메인 스레드에서 일어난다.** ``show``/``hide``는 메인
  스레드로 전달하고, 진행하기 전에 화면 표시 확인을 기다린다.

상태: 명세대로 구현되었으나 아직 화면에서 검증되지 않았다(손쉬운 사용 권한과 실제
전송이 필요하다). 취소는 키보드 우선(Esc)이다. 창은 마우스를 무시하므로 클릭 가능한
취소 버튼은 의도적으로 없다 — ADR-014 참조.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("sesametok_backend.send.curtain")

OVERLAY_ACK_SECONDS = 0.6
ACK_POLL_SECONDS = 0.005
WINDOW_LEVEL = 1_000_000
EVENT_TAG = 0x5E5A4D  # "SZM" — 우리가 직접 합성한 이벤트임을 표시한다

SWALLOWED_EVENTS = (
    "kCGEventKeyDown",
    "kCGEventKeyUp",
    "kCGEventFlagsChanged",
    "kCGEventLeftMouseDown",
    "kCGEventLeftMouseUp",
    "kCGEventRightMouseDown",
    "kCGEventRightMouseUp",
    "kCGEventOtherMouseDown",
    "kCGEventOtherMouseUp",
    "kCGEventScrollWheel",
    "kCGEventMouseMoved",
    "kCGEventLeftMouseDragged",
    "kCGEventRightMouseDragged",
)

MainDispatcher = Callable[[Callable[[], None]], None]


def _default_main_dispatcher(work: Callable[[], None]) -> None:
    """*work*를 AppKit 메인 스레드에서 실행하고, 끝날 때까지 기다린다."""

    done = threading.Event()
    error: list[BaseException] = []

    def wrapped() -> None:
        try:
            work()
        except BaseException as exc:  # pragma: no cover - 메인 스레드 실패
            error.append(exc)
        finally:
            done.set()

    try:
        from PyObjCTools import AppHelper  # type: ignore[import-not-found]

        AppHelper.callAfter(wrapped)
    except Exception as exc:  # pragma: no cover - AppKit을 사용할 수 없음
        raise RuntimeError("AppKit 메인 스레드에 접근할 수 없습니다") from exc

    if not done.wait(2.0):  # pragma: no cover - 메인 스레드가 멈춤
        raise RuntimeError("메인 스레드가 응답하지 않았습니다")
    if error:  # pragma: no cover
        raise error[0]


class Curtain:
    """활성화를 가로채지 않는 오버레이와 입력을 삼키는 이벤트 탭."""

    def __init__(
        self,
        *,
        dispatch: MainDispatcher | None = None,
        framework_loader: Callable[[], Any] | None = None,
        ack_seconds: float = OVERLAY_ACK_SECONDS,
    ) -> None:
        self._dispatch = dispatch or _default_main_dispatcher
        self._load = framework_loader or _load_frameworks
        self._ack_seconds = ack_seconds
        self._armed = threading.Event()
        self._shown = threading.Event()
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._window: Any = None
        self._tap: Any = None
        self._run_loop_source: Any = None
        self._framework_cache: Any = None

    # ------------------------------------------------------------- 상태

    def _framework(self) -> Any:
        if self._framework_cache is None:
            self._framework_cache = self._load()
        return self._framework_cache

    @property
    def armed(self) -> bool:
        """``show``가 시작된 순간부터 ``hide``가 끝날 때까지 True."""

        return self._armed.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def reset_cancel(self) -> None:
        self._cancelled.clear()

    # ------------------------------------------------------------- 제어

    def show(self) -> bool:
        """오버레이를 무장하고, 표시하고, 확인한다. ``False``는 진행하지 말라는 뜻이다."""

        with self._lock:
            self._armed.set()
            self._shown.clear()
            self.reset_cancel()
            try:
                self._dispatch(self._build)
            except Exception as exc:
                logger.warning("커튼을 표시하지 못했습니다: %s", type(exc).__name__)
                self._armed.clear()
                return False

            displayed = self._shown.wait(self._ack_seconds)
            if not displayed:
                logger.warning("커튼 표시가 확인되지 않았습니다")
                self._armed.clear()
                self._safe_teardown()
                return False
            return True

    def hide(self) -> None:
        with self._lock:
            self._armed.clear()
            self._safe_teardown()

    def _safe_teardown(self) -> None:
        try:
            self._dispatch(self._teardown)
        except Exception as exc:  # 멈춘 오버레이가 사용자를 가두어서는 안 된다
            logger.warning("커튼 해제에 실패했습니다: %s", type(exc).__name__)

    # ------------------------------------------------- 메인 스레드 본문

    def _build(self) -> None:
        """메인 스레드에서 실행된다."""

        framework = self._framework()
        try:
            panel = _make_panel(framework)
            self._window = panel
            self._tap, self._run_loop_source = _install_tap(framework, self._on_event)
            self._shown.set()
        except Exception:
            self._teardown_on_main()
            raise

    def _teardown(self) -> None:
        """메인 스레드에서 실행된다."""

        self._teardown_on_main()

    def _teardown_on_main(self) -> None:
        framework = self._framework()
        if self._tap is not None:
            try:
                framework.CGEventTapEnable(self._tap, False)
            except Exception:
                pass
            self._tap = None
        if self._run_loop_source is not None:
            try:
                framework.CFRunLoopRemoveSource(
                    framework.CFRunLoopGetCurrent(),
                    self._run_loop_source,
                    framework.kCFRunLoopCommonModes,
                )
            except Exception:
                pass
            self._run_loop_source = None
        if self._window is not None:
            try:
                self._window.orderOut_(None)
            except Exception:
                pass
            self._window = None
        self._shown.clear()

    # ------------------------------------------------------- 이벤트 처리

    def _on_event(self, _proxy, event_type, event, _refcon):
        """삼키려면 ``None``을, 통과시키려면 이벤트를 반환한다."""

        if not self._armed.is_set():
            return event

        framework = self._framework()
        try:
            tag = framework.CGEventGetIntegerValueField(event, framework.kCGEventSourceUserData)
        except Exception:
            tag = 0
        if tag == EVENT_TAG:
            return event  # 우리가 합성한 키 입력/클릭

        if framework_is_escape(framework, event_type, event):
            self._cancelled.set()
        # 그 밖에 사용자가 누른 것은 모두 큐에 쌓지 않고 삼킨다.
        return None


ESCAPE_KEYCODE = 53


def framework_is_escape(framework: Any, event_type: Any, event: Any) -> bool:
    """Esc의 key-down에만 True — 문서화된 취소 제스처."""

    if event_type != framework.kCGEventKeyDown:
        return False
    try:
        keycode = framework.CGEventGetIntegerValueField(
            event, framework.kCGKeyboardEventKeycode
        )
    except Exception:
        return False
    return int(keycode) == ESCAPE_KEYCODE


def _make_panel(framework: Any) -> Any:
    """활성화를 가로채지 않고, 마우스를 무시하며, 항상 맨 위에 있는 오버레이 패널."""

    NSWindow = framework.NSWindow
    style = (
        framework.NSWindowStyleMaskNonactivatingPanel
        | framework.NSWindowStyleMaskBorderless
    )
    rect = ((0.0, 0.0), (framework.NSScreen.mainScreen().frame().size.width, 90.0))
    panel = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, framework.NSBackingStoreBuffered, False
    )
    panel.setLevel_(WINDOW_LEVEL)
    panel.setOpaque_(False)
    panel.setIgnoresMouseEvents_(True)
    panel.setCollectionBehavior_(
        framework.NSWindowCollectionBehaviorCanJoinAllSpaces
        | framework.NSWindowCollectionBehaviorStationary
    )
    panel.setReleasedWhenClosed_(False)

    label = framework.NSTextField.alloc().initWithFrame_(rect)
    label.setStringValue_(
        "메시지를 보내는 동안 입력이 잠시 차단됩니다. 취소하려면 Esc를 누르세요."
    )
    label.setAlignment_(framework.NSTextAlignmentCenter)
    label.setTextColor_(framework.NSColor.whiteColor())
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    panel.contentView().addSubview_(label)

    panel.orderFrontRegardless()
    return panel


def _install_tap(framework: Any, callback: Callable) -> tuple[Any, Any]:
    mask = 0
    for name in SWALLOWED_EVENTS:
        mask |= framework.CGEventMaskBit(getattr(framework, name))

    tap = framework.CGEventTapCreate(
        framework.kCGSessionEventTap,
        framework.kCGHeadInsertEventTap,
        framework.kCGEventTapOptionDefault,
        mask,
        callback,
        None,
    )
    if tap is None:
        raise RuntimeError("이벤트 탭을 만들 수 없습니다")

    source = framework.CFMachPortCreateRunLoopSource(None, tap, 0)
    run_loop = framework.CFRunLoopGetCurrent()
    framework.CFRunLoopAddSource(run_loop, source, framework.kCFRunLoopCommonModes)
    framework.CGEventTapEnable(tap, True)
    return tap, source


def _load_frameworks() -> Any:
    """커튼에 필요한 AppKit/Quartz 표면을 임포트한다."""

    from types import SimpleNamespace

    import objc  # noqa: F401
    from AppKit import (  # type: ignore[import-not-found]
        NSBackingStoreBuffered,
        NSColor,
        NSScreen,
        NSTextAlignmentCenter,
        NSTextField,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Quartz import (  # type: ignore[import-not-found]
        CFMachPortCreateRunLoopSource,
        CFRunLoopAddSource,
        CFRunLoopGetCurrent,
        CFRunLoopRemoveSource,
        CGEventGetIntegerValueField,
        CGEventMaskBit,
        CGEventTapCreate,
        CGEventTapEnable,
        kCFRunLoopCommonModes,
        kCGEventFlagsChanged,
        kCGEventKeyDown,
        kCGEventKeyUp,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseDragged,
        kCGEventLeftMouseUp,
        kCGEventMouseMoved,
        kCGEventOtherMouseDown,
        kCGEventOtherMouseUp,
        kCGEventRightMouseDown,
        kCGEventRightMouseDragged,
        kCGEventRightMouseUp,
        kCGEventScrollWheel,
        kCGEventSourceUserData,
        kCGEventTapOptionDefault,
        kCGHeadInsertEventTap,
        kCGKeyboardEventKeycode,
        kCGSessionEventTap,
    )

    return SimpleNamespace(**{name: value for name, value in locals().items() if name != "objc"})
