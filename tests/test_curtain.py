"""커튼 테스트.

화면에 실제로 나타나는 모습은 실제 세션이 필요하지만, 표시 확인 규칙, 실패 시
닫히는(fail-closed) 동작, 이벤트 삼키기 정책은 가짜 프레임워크로 모두 검증할 수 있다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from sesametok_backend.send import curtain as curtain_module
from sesametok_backend.send.curtain import EVENT_TAG, Curtain, framework_is_escape

KEY_DOWN = "keydown"
KEY_UP = "keyup"
TAGGED = "tagged"


class FakePanel:
    def __init__(self) -> None:
        self.visible = False

    def setLevel_(self, *_): return None

    def setOpaque_(self, *_): return None

    def setIgnoresMouseEvents_(self, *_): return None

    def setCollectionBehavior_(self, *_): return None

    def setReleasedWhenClosed_(self, *_): return None

    def contentView(self): return self

    def addSubview_(self, *_): return None

    def orderFrontRegardless(self): self.visible = True

    def orderOut_(self, *_): self.visible = False


class FakeLabel:
    def setStringValue_(self, *_): return None

    def setAlignment_(self, *_): return None

    def setTextColor_(self, *_): return None

    def setBezeled_(self, *_): return None

    def setDrawsBackground_(self, *_): return None

    def setEditable_(self, *_): return None


class FakeFactory:
    def alloc(self): return self

    def initWithContentRect_styleMask_backing_defer_(self, *_): return FakePanel()

    def initWithFrame_(self, *_): return FakeLabel()


class FakeScreen:
    def frame(self):
        return SimpleNamespace(size=SimpleNamespace(width=1440.0))


def fake_framework(*, escape_keycode: int = 53) -> SimpleNamespace:
    events = SimpleNamespace(
        tagged={},
        user_keycode=escape_keycode,
        user_data=0,
    )

    def make_event(kind: str):
        return {"kind": kind}

    framework = SimpleNamespace(
        NSWindow=FakeFactory(),
        NSTextField=FakeFactory(),
        NSColor=SimpleNamespace(whiteColor=lambda: "white"),
        NSScreen=SimpleNamespace(mainScreen=lambda: FakeScreen()),
        NSTextAlignmentCenter=1,
        NSWindowStyleMaskNonactivatingPanel=1,
        NSWindowStyleMaskBorderless=2,
        NSBackingStoreBuffered=2,
        NSWindowCollectionBehaviorCanJoinAllSpaces=4,
        NSWindowCollectionBehaviorStationary=8,
        events=events,
    )

    for name in curtain_module.SWALLOWED_EVENTS:
        setattr(framework, name, name)
    # 테스트가 이벤트 타입으로 넘기는 것과 같은 토큰을 쓴다.
    framework.kCGEventKeyDown = KEY_DOWN
    framework.kCGEventKeyUp = KEY_UP
    framework.kCFRunLoopCommonModes = "common"
    framework.kCGSessionEventTap = "session"
    framework.kCGHeadInsertEventTap = "head"
    framework.kCGEventTapOptionDefault = "default"
    framework.kCGEventSourceUserData = "userdata"
    framework.kCGKeyboardEventKeycode = "keycode"

    framework.CGEventMaskBit = lambda value: 1 << 1
    framework.CGEventTapCreate = lambda *a, **k: "tap"
    framework.CGEventTapEnable = lambda *a, **k: None
    framework.CFMachPortCreateRunLoopSource = lambda *a, **k: "source"
    framework.CFRunLoopGetCurrent = lambda: "runloop"
    framework.CFRunLoopAddSource = lambda *a, **k: None
    framework.CFRunLoopRemoveSource = lambda *a, **k: None

    def get_integer(event, field):
        if field == framework.kCGEventSourceUserData:
            return events.user_data
        if field == framework.kCGKeyboardEventKeycode:
            return events.user_keycode
        return 0

    framework.CGEventGetIntegerValueField = get_integer
    return framework


def make_curtain(framework, *, dispatch=None, ack_seconds: float = 0.2) -> Curtain:
    return Curtain(
        dispatch=dispatch or (lambda work: work()),
        framework_loader=lambda: framework,
        ack_seconds=ack_seconds,
    )


def test_show_arms_then_confirms_display() -> None:
    framework = fake_framework()
    curtain = make_curtain(framework)

    assert curtain.show() is True
    assert curtain.armed is True

    curtain.hide()
    assert curtain.armed is False


def test_show_is_armed_before_it_displays() -> None:
    seen: list[bool] = []

    def dispatch(work):
        seen.append(curtain.armed)
        # 일부러 work를 실행하지 않는다. 표시가 끝나지 않는다.

    framework = fake_framework()
    curtain = Curtain(
        dispatch=dispatch, framework_loader=lambda: framework, ack_seconds=0.05
    )

    assert curtain.show() is False
    # 표시가 시작될 때 이미 armed 플래그가 켜져 있었다(문서화된 규칙).
    assert seen[0] is True
    assert curtain.armed is False


def test_show_fails_closed_when_display_raises() -> None:
    def dispatch(work):
        raise RuntimeError("메인 스레드 없음")

    framework = fake_framework()
    curtain = Curtain(
        dispatch=dispatch, framework_loader=lambda: framework, ack_seconds=0.05
    )

    assert curtain.show() is False
    assert curtain.armed is False


def test_hide_is_idempotent() -> None:
    framework = fake_framework()
    curtain = make_curtain(framework)

    curtain.show()
    curtain.hide()
    curtain.hide()

    assert curtain.armed is False
    assert curtain.cancelled is False


def test_user_input_is_swallowed_while_armed() -> None:
    framework = fake_framework()
    curtain = make_curtain(framework)
    curtain.show()

    framework.events.user_data = 0
    event = {"kind": KEY_DOWN}
    assert curtain._on_event(None, KEY_DOWN, event, None) is None


def test_tagged_synthetic_input_passes_through() -> None:
    framework = fake_framework()
    curtain = make_curtain(framework)
    curtain.show()

    framework.events.user_data = EVENT_TAG
    event = {"kind": TAGGED}
    assert curtain._on_event(None, KEY_DOWN, event, None) is event


def test_input_passes_through_when_disarmed() -> None:
    framework = fake_framework()
    curtain = make_curtain(framework)
    event = {"kind": KEY_DOWN}

    assert curtain._on_event(None, KEY_DOWN, event, None) is event


def test_escape_cancels_but_other_keys_do_not() -> None:
    framework = fake_framework(escape_keycode=53)
    curtain = make_curtain(framework)
    curtain.show()

    framework.events.user_keycode = 36  # Enter
    curtain._on_event(None, KEY_DOWN, {"kind": KEY_DOWN}, None)
    assert curtain.cancelled is False

    framework.events.user_keycode = 53  # Esc
    curtain._on_event(None, KEY_DOWN, {"kind": KEY_DOWN}, None)
    assert curtain.cancelled is True


def test_escape_detection_requires_key_down() -> None:
    framework = fake_framework()
    assert framework_is_escape(framework, KEY_DOWN, {}) is True
    assert framework_is_escape(framework, KEY_UP, {}) is False


def test_reset_cancel_clears_the_flag() -> None:
    framework = fake_framework()
    curtain = make_curtain(framework)
    curtain.show()
    framework.events.user_keycode = 53
    curtain._on_event(None, KEY_DOWN, {}, None)

    curtain.reset_cancel()

    assert curtain.cancelled is False
