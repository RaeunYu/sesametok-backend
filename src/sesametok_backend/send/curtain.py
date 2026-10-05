"""The input-blocking curtain.

Bringing KakaoTalk forward for an image paste or a real double-click means the
user's next keystroke could land in the wrong application. While the curtain is
up, a session-level ``CGEventTap`` swallows user input and only lets through
events this process synthesised (tagged in ``kCGEventSourceUserData``).

Three rules are load-bearing:

* **The curtain is never invisible.** Swallowed keys are discarded, not queued, so
  silently blocking input would be unsafe. If it cannot be shown, ``show()``
  returns ``False`` and the caller must not take the visible path.
* **The window must not become key.** If our process activates, KakaoTalk loses
  frontmost and the paste dies.
* **All AppKit work happens on the main thread.** ``show``/``hide`` marshal there
  and wait for the display acknowledgement before proceeding.

Status: implemented to spec, not yet verified on screen (needs Accessibility
permission and a live send). Cancel is keyboard-first (Esc); the window ignores
the mouse, so a clickable cancel button is intentionally absent — see ADR-014.
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
EVENT_TAG = 0x5E5A4D  # "SZM" — marks events we synthesised ourselves

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
    """Run *work* on the AppKit main thread, waiting for it to finish."""

    done = threading.Event()
    error: list[BaseException] = []

    def wrapped() -> None:
        try:
            work()
        except BaseException as exc:  # pragma: no cover - main-thread failure
            error.append(exc)
        finally:
            done.set()

    try:
        from PyObjCTools import AppHelper  # type: ignore[import-not-found]

        AppHelper.callAfter(wrapped)
    except Exception as exc:  # pragma: no cover - no AppKit available
        raise RuntimeError("cannot reach the AppKit main thread") from exc

    if not done.wait(2.0):  # pragma: no cover - main thread is wedged
        raise RuntimeError("the main thread did not respond")
    if error:  # pragma: no cover
        raise error[0]


class Curtain:
    """A non-activating overlay plus an input-swallowing event tap."""

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

    # ------------------------------------------------------------- state

    def _framework(self) -> Any:
        if self._framework_cache is None:
            self._framework_cache = self._load()
        return self._framework_cache

    @property
    def armed(self) -> bool:
        """True from the moment ``show`` starts until ``hide`` finishes."""

        return self._armed.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def reset_cancel(self) -> None:
        self._cancelled.clear()

    # ------------------------------------------------------------- control

    def show(self) -> bool:
        """Arm, display, and confirm the overlay. ``False`` means do not proceed."""

        with self._lock:
            self._armed.set()
            self._shown.clear()
            self.reset_cancel()
            try:
                self._dispatch(self._build)
            except Exception as exc:
                logger.warning("curtain could not be shown: %s", type(exc).__name__)
                self._armed.clear()
                return False

            displayed = self._shown.wait(self._ack_seconds)
            if not displayed:
                logger.warning("curtain display was not acknowledged")
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
        except Exception as exc:  # a stuck overlay must not trap the user
            logger.warning("curtain teardown failed: %s", type(exc).__name__)

    # ------------------------------------------------- main-thread bodies

    def _build(self) -> None:
        """Runs on the main thread."""

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
        """Runs on the main thread."""

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

    # ------------------------------------------------------- event handling

    def _on_event(self, _proxy, event_type, event, _refcon):
        """Return ``None`` to swallow, or the event to let it through."""

        if not self._armed.is_set():
            return event

        framework = self._framework()
        try:
            tag = framework.CGEventGetIntegerValueField(event, framework.kCGEventSourceUserData)
        except Exception:
            tag = 0
        if tag == EVENT_TAG:
            return event  # our own synthesised keystroke/click

        if framework_is_escape(framework, event_type, event):
            self._cancelled.set()
        # Everything else the user pressed is swallowed, not queued.
        return None


ESCAPE_KEYCODE = 53


def framework_is_escape(framework: Any, event_type: Any, event: Any) -> bool:
    """True only for a key-down of Esc — the documented cancel gesture."""

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
    """A non-activating, mouse-ignoring, always-on-top overlay panel."""

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
        raise RuntimeError("the event tap could not be created")

    source = framework.CFMachPortCreateRunLoopSource(None, tap, 0)
    run_loop = framework.CFRunLoopGetCurrent()
    framework.CFRunLoopAddSource(run_loop, source, framework.kCFRunLoopCommonModes)
    framework.CGEventTapEnable(tap, True)
    return tap, source


def _load_frameworks() -> Any:
    """Import the AppKit/Quartz surface the curtain needs."""

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
