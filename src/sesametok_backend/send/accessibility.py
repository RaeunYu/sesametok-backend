"""Driving the real KakaoTalk window through macOS Accessibility.

The essence of this module is *order and observation*: every step either observes
the UI or posts a keystroke, and the moment a value does not hold what was
written, the pipeline stops instead of acting on the wrong thing. Sending is
irreversible, so the default is to refuse on ambiguity.

Status: the order, the gates, the timing constants, and the error codes are
implemented as specified. Live verification requires Accessibility permission
(currently granted to the interpreter that runs the backend) and a room the
operator names; until that test runs, treat this file as unverified.

The module imports pyobjc lazily so the rest of the backend (and the test suite)
works headlessly and on machines without a GUI session.
"""

from __future__ import annotations

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

# ---------------------------------------------------------------------- constants

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
MAX_DESCENDANTS = 4000

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


@dataclass(frozen=True)
class WindowInfo:
    index: int
    title: str
    element: Any


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def _wait_for(predicate, attempts: int, interval: float):
    """Poll *predicate* for up to ``attempts × interval`` seconds."""

    for _ in range(max(1, attempts)):
        value = predicate()
        if value:
            return value
        _sleep(interval)
    return None


def _load_frameworks() -> SimpleNamespace:
    """Import the macOS frameworks, or fail with the permission error code."""

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
        )
        from Quartz import (  # type: ignore[import-not-found]
            CGEventCreateKeyboardEvent,
            CGEventPost,
            CGEventSetFlags,
            CGEventSourceSecondsSinceLastEventType,
            kCGEventFlagMaskCommand,
            kCGHIDEventTap,
            kCGEventSourceStateCombinedSessionState,
        )
    except Exception as exc:  # pragma: no cover - depends on the machine
        raise SendError(
            "accessibility_denied",
            f"the macOS Accessibility frameworks are unavailable: {exc}",
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
        KeyEvent=CGEventCreateKeyboardEvent,
        KeyPost=CGEventPost,
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


def _element_center(framework: SimpleNamespace, element: Any) -> tuple[float, float] | None:
    from Quartz import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType

    position = _attr(framework, element, "AXPosition")
    size = _attr(framework, element, "AXSize")
    if position is None or size is None:
        return None
    ok_point, point = AXValueGetValue(position, kAXValueCGPointType, None)
    ok_size, dimensions = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (ok_point and ok_size):
        return None
    return (point.x + dimensions.width / 2.0, point.y + dimensions.height / 2.0)


class KakaoTalkDriver:
    """The single-threaded Accessibility driver."""

    def __init__(self, *, background_only: bool = False, curtain: Any = None) -> None:
        self.background_only = background_only
        self.framework = _load_frameworks()
        self.curtain = curtain
        self._pid: int | None = None

    # ------------------------------------------------------------- preconditions

    def _require_permission(self) -> None:
        if not bool(self.framework.AXIsProcessTrusted()):
            raise SendError("accessibility_denied")

    def _require_running(self) -> int:
        try:
            result = subprocess.run(
                ["pgrep", "-x", "KakaoTalk"], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SendError("not_running", f"could not run pgrep: {exc}") from exc
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        if not pids:
            raise KakaoTalkNotRunning()
        self._pid = int(pids[0])
        return self._pid

    def _app_element(self):
        assert self._pid is not None
        return self.framework.CreateApp(self._pid)

    # ------------------------------------------------------------------- windows

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
        """Resolve exactly one window for *target* or refuse (never guess)."""

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

    def _open_room(self, app, target: SendTarget):
        """Try to open the room via search, then the chat list."""

        open_titles = ", ".join(w.title for w in self._windows(app))
        last_error: SendError | None = None
        for attempt in range(OPEN_ATTEMPTS):
            strategy = attempt % 3
            try:
                if strategy == 0 and self._open_via_search(app, target):
                    return self._wait_for_single_window(app, target)
                if strategy == 1 and self._open_via_chat_list(app, target, recent_only=True):
                    return self._wait_for_single_window(app, target)
                if strategy == 2 and self._open_via_chat_list(app, target, recent_only=False):
                    return self._wait_for_single_window(app, target)
            except SendError as exc:
                if exc.code in ("ambiguous_room", "room_name_not_typable", "chat_list_moved"):
                    raise  # never retry an ambiguity
                last_error = exc
            _sleep(0.1)

        if last_error is not None:
            raise last_error
        raise SendError("room_open_failed", room=target.room_name, open=open_titles)

    def _wait_for_single_window(self, app, target: SendTarget):
        def single():
            matches = self._matching_windows(app, target.room_name)
            if len(matches) > 1:
                raise SendError("ambiguous_room", room=target.room_name, count=len(matches))
            return matches[0].element if matches else None

        window = _wait_for(single, *WINDOW_APPEAR_WAIT)
        if window is None:
            raise SendError("room_open_failed", room=target.room_name)
        return window

    def _descendants(self, root) -> list[Any]:
        found: list[Any] = []
        stack = [root]
        while stack and len(found) < MAX_DESCENDANTS:
            element = stack.pop()
            children = _attr(self.framework, element, "AXChildren") or []
            for child in children:
                found.append(child)
                stack.append(child)
        return found

    def _search_field(self, app):
        for element in self._descendants(app):
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
        rows = self._rows(app)
        if not rows:
            raise SendError("chat_list_unavailable")
        candidates = rows[:RECENT_ROWS] if recent_only else rows

        matching: list[tuple[int, Any, str]] = []
        for position, row in enumerate(candidates):
            title = self._row_title(row)
            if title and room_matches(title, target.room_name):
                matching.append((position, row, title))

        if not matching:
            return False
        if len(matching) > 1:
            raise SendError("ambiguous_room", room=target.room_name, count=len(matching))

        position, row, title = matching[0]
        # Re-read the row we are about to click: a reorder means the click would
        # land on whichever room took that position.
        current = self._rows(app)
        if len(current) <= position or not room_matches(self._row_title(current[position]) or "", title):
            raise SendError("chat_list_moved", room=target.room_name)
        self._press(current[position])
        return True

    def _rows(self, app) -> list[Any]:
        rows: list[Any] = []
        for element in self._descendants(app):
            role = _text_attr(self.framework, element, "AXRole") or ""
            if role in ("AXRow", "AXOutlineRow"):
                rows.append(element)
        return rows

    def _row_title(self, row) -> str | None:
        for element in [row, *self._descendants(row)]:
            role = _text_attr(self.framework, element, "AXRole") or ""
            if role in ("AXStaticText", "AXCell", "AXTextField"):
                value = _text_attr(self.framework, element, "AXValue") or _text_attr(
                    self.framework, element, "AXTitle"
                )
                if value:
                    return value
        return None

    # ---------------------------------------------------------------- elements

    def _find_compose_box(self, window):
        """``AXScrollArea > AXTextArea`` inside the window."""

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

    def _raise(self, element) -> None:
        self.framework.PerformAction(element, "AXRaise")

    # ---------------------------------------------------------------- input

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

    # ------------------------------------------------------------- focus policy

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
        """Bring KakaoTalk forward and verify it stayed frontmost."""

        self.framework.NSWorkspace.sharedWorkspace()  # keep the framework warm
        activate = getattr(self.framework.NSApplication, "sharedApplication", None)
        if activate is not None:
            # Best effort: raise the app via Accessibility, then verify.
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

    # ------------------------------------------------------------ public API

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
            self._post_key(KEY_ENTER)
            if self._compose_emptied(compose):
                return SendOutcome("sent")
            return SendOutcome(
                "unconfirmed",
                "background_send_not_accepted",
                str(SendError("background_send_not_accepted")),
            )

        # Visible path: take focus only now, then observe.
        if self._send_visible(self._app_element(), window, compose, text):
            return SendOutcome("sent")
        return SendOutcome("unconfirmed", "not_sent", str(SendError("not_sent")))

    def _compose_emptied(self, compose) -> bool:
        return _wait_for(lambda: self._value(compose) == "", *COMPOSE_DRAIN) is not None

    def _send_visible(self, app, window, compose, text: str) -> bool:
        """Steps 14–17: wait for idle, take the screen, then Enter / click Send.

        The input curtain is raised first when available. If it cannot be
        displayed we continue **unblocked** (the documented fallback) but keep
        every frontmost check in place; blocking invisibly is never allowed.
        """

        shown = self._raise_curtain()
        try:
            self._wait_for_idle()
            if self._curtain_cancelled():
                raise SendError("cancelled")
            self._activate(app)          # step 14
            self._raise(window)          # step 15
            _sleep(RAISE_DELAY)
            self._set_focus(compose)
            if self._value(compose) != text:
                raise SendError("value_mismatch", what="the compose box")

            self._post_key(KEY_ENTER)    # step 16
            if self._compose_emptied(compose):
                return True

            button = self._find_button(window, SEND_BUTTON_TITLES)
            if button is not None:       # step 17
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
            logger.warning("curtain show failed (%s); continuing unblocked", type(exc).__name__)
            return False
        if not shown:
            logger.warning("curtain was not acknowledged; continuing unblocked")
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
        """A draft must never use ``AXValue``: writing it can itself send."""

        if self.background_only:
            # Staging a draft pastes into the frontmost app, which background-only
            # mode forbids.
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
        # Never post Cmd+V without re-confirming frontmost.
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
                "bad_request", f"unsupported image type {extension!r}"
            )
        self._require_permission()
        self._require_running()

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
            raise SendError("bad_request", "the image file could not be read")
        pasteboard.writeObjects_([image])

        if background_only:
            # Pasting an image needs a global Cmd+V into the frontmost app.
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
        from ..core.send_queue import SendOutcome  # noqa: F401 - consistency

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
        """Cancel an in-flight visible send (Esc is only valid once shown)."""

        self._post_key(KEY_ESC)
