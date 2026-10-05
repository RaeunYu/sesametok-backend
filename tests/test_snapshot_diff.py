from __future__ import annotations

from sesametok_backend.core.snapshot import (
    diff_messages,
    diff_rooms,
    message_key,
    room_fingerprint,
)
from sesametok_backend.models import Message, Room


def _message(message_id: str = "100-1", **overrides) -> Message:
    base = dict(
        message_id=message_id,
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
    return Message(**base)


def test_new_keys_are_created() -> None:
    diff = diff_messages({}, [_message()])

    assert len(diff.created) == 1
    assert diff.updated == ()
    assert diff.unchanged == 0


def test_identical_message_is_silent() -> None:
    message = _message()
    existing = {message_key(message): message.fingerprint()}

    diff = diff_messages(existing, [message])

    assert diff.created == ()
    assert diff.updated == ()
    assert diff.unchanged == 1


def test_changed_body_is_updated() -> None:
    original = _message()
    edited = _message(text="edited")
    existing = {message_key(original): original.fingerprint()}

    diff = diff_messages(existing, [edited])

    assert diff.created == ()
    assert [m.text for m in diff.updated] == ["edited"]


def test_first_sighting_wins_for_duplicates() -> None:
    first = _message(text="first")
    second = _message(text="second")

    diff = diff_messages({}, [first, second])

    assert len(diff.created) == 1
    assert diff.created[0].text == "first"


def test_placeholder_flag_change_is_an_update() -> None:
    plain = _message()
    substituted = _message(text="[placeholder]", original_message_type="type_2", is_placeholder=True)

    assert plain.fingerprint() != substituted.fingerprint()


def test_room_diff_detects_rename_and_new_rooms() -> None:
    existing = {"100": ("old name", "direct")}
    rooms = [
        Room("100", "new name", "direct"),
        Room("200", "fresh", "group"),
    ]

    diff = diff_rooms(existing, rooms)

    assert {room.chat_id for room in diff.changed} == {"100", "200"}


def test_room_diff_ignores_unchanged() -> None:
    room = Room("100", "same", "group")
    diff = diff_rooms({"100": room_fingerprint(room)}, [room])
    assert diff.changed == ()
