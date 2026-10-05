"""Guard tests for the Accessibility driver.

The UI-driving paths need a real screen and permission; these tests cover the
fail-closed behaviour and the focus policy with a fake framework, so they run
headlessly.
"""

from __future__ import annotations

import pytest

from sesametok_backend.send import accessibility as ax
from sesametok_backend.send.errors import SendError
from sesametok_backend.send.targets import SendTarget

TARGET = SendTarget(chat_id="100", room_name="Team Room", rank=0, chat_type="group")


class FakeFramework:
    """The smallest surface the driver touches, with no macOS involved."""

    HIDTap = "hid"
    CombinedState = "combined"

    def __init__(self, *, trusted: bool = False, seconds_since_input: float = 5.0) -> None:
        self.trusted = trusted
        self.seconds_since_input = seconds_since_input
        self.attributes: dict[tuple[int, str], object] = {}
        self.posted: list[int] = []
        self.actions: list[tuple[int, str]] = []
        self.set_values: list[tuple[int, str, object]] = []

    # --- framework API -------------------------------------------------
    def AXIsProcessTrusted(self) -> bool:
        return self.trusted

    def CreateApp(self, pid: int):
        return {"pid": pid}

    def CopyAttr(self, element, name, _):
        if isinstance(element, dict) and "pid" in element and name == "AXWindows":
            return 0, []
        return 0, self.attributes.get((id(element), name))

    def PerformAction(self, element, action):
        self.actions.append((id(element), action))
        return 0

    def SetAttr(self, element, name, value):
        self.set_values.append((id(element), name, value))
        return 0

    def KeyEvent(self, source, keycode, down):
        return {"keycode": keycode, "down": down}

    def KeyPost(self, tap, event):
        self.posted.append(event["keycode"])

    def SetFlags(self, event, flags):
        return None

    def SecondsSinceLastEvent(self, state, mask):
        return self.seconds_since_input


def _driver(monkeypatch, framework: FakeFramework) -> ax.KakaoTalkDriver:
    monkeypatch.setattr(ax, "_load_frameworks", lambda: framework)
    driver = ax.KakaoTalkDriver()
    driver._pid = 4242
    return driver


def test_module_imports_without_appkit() -> None:
    assert ax.KEY_ENTER == 36
    assert ax.KEY_CMD_V == 9
    assert ax.KEY_ESC == 53
    assert ax.IMAGE_UTIS[".png"] == "public.png"


def test_missing_permission_refuses_before_anything_else(monkeypatch) -> None:
    driver = _driver(monkeypatch, FakeFramework(trusted=False))

    with pytest.raises(SendError) as excinfo:
        driver.send_text(TARGET, "hi", mode="send", background_only=True)

    assert excinfo.value.code == "accessibility_denied"


def test_idle_gap_waits_until_the_user_stops(monkeypatch) -> None:
    framework = FakeFramework(trusted=True, seconds_since_input=0.0)
    monkeypatch.setattr(ax, "IDLE_MAX_SECONDS", 0.05)
    monkeypatch.setattr(ax, "IDLE_POLL_SECONDS", 0.005)
    driver = _driver(monkeypatch, framework)

    with pytest.raises(SendError) as excinfo:
        driver._wait_for_idle()

    assert excinfo.value.code == "user_busy"


def test_idle_gap_returns_immediately_when_quiet(monkeypatch) -> None:
    framework = FakeFramework(trusted=True, seconds_since_input=5.0)
    driver = _driver(monkeypatch, framework)

    driver._wait_for_idle()  # must not raise


def test_idle_gap_can_be_skipped(monkeypatch) -> None:
    framework = FakeFramework(trusted=True, seconds_since_input=0.0)
    monkeypatch.setattr(ax, "IDLE_MAX_SECONDS", 0.01)
    driver = _driver(monkeypatch, framework)

    driver._wait_for_idle(immediate=True)  # must not raise


def test_background_only_refuses_to_open_a_closed_room(monkeypatch) -> None:
    driver = _driver(monkeypatch, FakeFramework(trusted=True))
    driver.background_only = True

    with pytest.raises(SendError) as excinfo:
        driver.find_window(TARGET)

    assert excinfo.value.code == "window_not_open"


def test_ambiguous_windows_are_refused(monkeypatch) -> None:
    framework = FakeFramework(trusted=True)
    window_a, window_b = object(), object()
    framework.attributes[(id(window_a), "AXTitle")] = "Team Room"
    framework.attributes[(id(window_b), "AXTitle")] = "Team Room"
    framework.CopyAttr = lambda element, name, _: (
        (0, [window_a, window_b])
        if isinstance(element, dict) and name == "AXWindows"
        else (0, framework.attributes.get((id(element), name)))
    )
    driver = _driver(monkeypatch, framework)

    with pytest.raises(SendError) as excinfo:
        driver.find_window(TARGET)

    assert excinfo.value.code == "ambiguous_room"


def test_set_value_failure_is_reported(monkeypatch) -> None:
    framework = FakeFramework(trusted=True)
    framework.SetAttr = lambda element, name, value: -25204
    driver = _driver(monkeypatch, framework)
    compose = object()

    with pytest.raises(SendError) as excinfo:
        driver._set_value(compose, "hello")

    assert excinfo.value.code == "set_value_failed"


def test_cancel_posts_escape(monkeypatch) -> None:
    framework = FakeFramework(trusted=True)
    driver = _driver(monkeypatch, framework)

    driver.cancel()

    # Key down and key up.
    assert framework.posted == [ax.KEY_ESC, ax.KEY_ESC]
