from __future__ import annotations

from sesametok_backend.models import EVENT_KINDS, Event, Message, PLACEHOLDER_TEXT, Room


def _message(**overrides: object) -> Message:
    base = dict(
        message_id="100-10",
        chat_id="100",
        chat_name="room",
        chat_type="direct",
        sender_id="500",
        sender_nickname="nick",
        timestamp="2026-01-01T09:00:00+00:00",
        text="hello",
        message_type="text",
    )
    base.update(overrides)
    return Message(**base)  # type: ignore[arg-type]


def test_placeholder_text_is_the_agreed_wording() -> None:
    assert PLACEHOLDER_TEXT == "[지원하지 않는 메시지 타입입니다. 원본 메시지를 확인해주세요.]"


def test_fingerprint_changes_with_body() -> None:
    assert _message().fingerprint() != _message(text="edited").fingerprint()


def test_fingerprint_changes_with_placeholder_flags() -> None:
    plain = _message()
    substituted = _message(
        text=PLACEHOLDER_TEXT, original_message_type="type_2", is_placeholder=True
    )
    assert plain.fingerprint() != substituted.fingerprint()


def test_fingerprint_is_stable_for_equal_messages() -> None:
    assert _message().fingerprint() == _message().fingerprint()


def test_message_to_dict_has_the_documented_fields() -> None:
    data = _message().to_dict()
    assert set(data) >= {
        "message_id",
        "chat_id",
        "chat_name",
        "chat_type",
        "sender_id",
        "sender_nickname",
        "timestamp",
        "text",
        "message_type",
        "reply_to_message_id",
        "is_feed",
    }
    assert data["original_message_type"] is None
    assert data["is_placeholder"] is False


def test_room_round_trips() -> None:
    room = Room("100", "room", "group", "2026-01-01T09:00:00+00:00", 3)
    assert room.to_dict()["message_count"] == 3


def test_event_kinds_are_fixed() -> None:
    assert EVENT_KINDS == {"message.created", "message.updated", "room.updated", "send.result"}
    event = Event(seq=7, kind="message.created", payload={"chat_id": "100"})
    assert event.to_dict()["seq"] == 7
    assert "created_at" not in event.to_dict()
