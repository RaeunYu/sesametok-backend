from __future__ import annotations

import pytest

from sesametok_backend.models import Room
from sesametok_backend.send.errors import SendError, format_message
from sesametok_backend.send.targets import (
    TargetError,
    resolve_target,
    room_matches,
    room_member_key,
    search_query,
    untypable_characters,
    validate_room_name,
)


def _room(chat_id: str, name: str, *, stamp: str | None = None, kind: str = "group") -> Room:
    return Room(chat_id, name, kind, stamp)


# --------------------------------------------------------------- name matching


def test_member_set_matching_ignores_order() -> None:
    assert room_matches("Bob, Alice, Carol", "Carol, Bob, Alice") is True
    assert room_matches("Alice, Bob", "Alice, Carol") is False


def test_exact_match_without_commas() -> None:
    assert room_matches("Team Standup", "Team Standup") is True
    assert room_matches("Team Standup", "team standup") is False


def test_member_key_strips_blank_parts() -> None:
    assert room_member_key(" Alice , , Bob ") == ["Alice", "Bob"]


def test_search_query_uses_the_longest_member() -> None:
    assert search_query("Al, Bobby, Christina") == "Christina"
    assert search_query("a, bb, cc") == "bb"
    assert search_query("Solo Room") == "Solo Room"


# ----------------------------------------------------------- name typability


@pytest.mark.parametrize("name", ["normal name", "이름 with spaces", "emoji 🎉 ok"])
def test_typable_names_pass(name: str) -> None:
    validate_room_name(name)
    assert untypable_characters(name) == []


@pytest.mark.parametrize(
    "name",
    [
        "bad\u200bname",       # zero width space
        "bad\u00adname",       # soft hyphen
        "bad\u202ename",       # right-to-left override
        "bad\u2060name",       # word joiner
        "bad\ufeffname",       # byte order mark
        "line\nbreak",         # control character
    ],
)
def test_untypable_names_are_rejected(name: str) -> None:
    with pytest.raises(TargetError) as excinfo:
        validate_room_name(name)
    assert excinfo.value.code == "room_name_not_typable"
    assert "U+" in str(excinfo.value.details["offenders"])


def test_offenders_are_listed_once() -> None:
    assert untypable_characters("\u200b\u200b") == ["U+200B"]


# ------------------------------------------------------------ rank resolution


def test_unique_name_gets_rank_zero() -> None:
    rooms = [_room("1", "Only Room", stamp="2026-01-01T00:00:00+00:00")]
    target = resolve_target(rooms, "1")

    assert (target.room_name, target.rank) == ("Only Room", 0)


def test_rank_counts_newer_same_named_rooms() -> None:
    rooms = [
        _room("1", "Shared", stamp="2026-01-03T00:00:00+00:00"),
        _room("2", "Shared", stamp="2026-01-02T00:00:00+00:00"),
        _room("3", "Shared", stamp="2026-01-01T00:00:00+00:00"),
    ]

    assert resolve_target(rooms, "1").rank == 0
    assert resolve_target(rooms, "2").rank == 1
    assert resolve_target(rooms, "3").rank == 2


def test_rank_uses_member_set_variants() -> None:
    rooms = [
        _room("1", "Alice, Bob", stamp="2026-01-02T00:00:00+00:00"),
        _room("2", "Bob, Alice", stamp="2026-01-01T00:00:00+00:00"),
    ]

    assert resolve_target(rooms, "1").rank == 0
    assert resolve_target(rooms, "2").rank == 1


def test_rooms_with_no_messages_sort_last() -> None:
    rooms = [
        _room("1", "Shared", stamp=None),
        _room("2", "Shared", stamp="2026-01-01T00:00:00+00:00"),
    ]

    assert resolve_target(rooms, "2").rank == 0
    assert resolve_target(rooms, "1").rank == 1


def test_equal_recency_is_ambiguous() -> None:
    rooms = [
        _room("1", "Shared", stamp="2026-01-01T00:00:00+00:00"),
        _room("2", "Shared", stamp="2026-01-01T00:00:00+00:00"),
    ]

    with pytest.raises(TargetError) as excinfo:
        resolve_target(rooms, "1")
    assert excinfo.value.code == "target_ambiguous"


def test_two_rooms_with_no_messages_are_ambiguous() -> None:
    rooms = [_room("1", "Shared", stamp=None), _room("2", "Shared", stamp=None)]

    with pytest.raises(TargetError) as excinfo:
        resolve_target(rooms, "1")
    assert excinfo.value.code == "target_ambiguous"


def test_unknown_chat_id_is_rejected() -> None:
    with pytest.raises(TargetError) as excinfo:
        resolve_target([_room("1", "Room")], "999")
    assert excinfo.value.code == "room_not_found"


def test_untypable_target_name_is_rejected() -> None:
    rooms = [_room("1", "bad\u200bname")]
    with pytest.raises(TargetError) as excinfo:
        resolve_target(rooms, "1")
    assert excinfo.value.code == "room_name_not_typable"


# ------------------------------------------------------------ error messages


def test_error_messages_carry_the_details() -> None:
    message = format_message("window_not_open", room="Team", open="A, B")
    assert "Team" in message and "A, B" in message

    busy = format_message("user_busy", n=7)
    assert "7s" in busy


def test_error_payload_shape() -> None:
    error = SendError("accessibility_denied", retryable=False)
    assert error.code == "accessibility_denied"
    assert "System Settings" in str(error)


def test_cancelled_messages_distinguish_the_two_cases() -> None:
    assert "nothing was sent" in format_message("cancelled")
    assert "may or may not" in format_message("cancelled_after_paste")


def test_unconfirmed_warns_against_blind_retry() -> None:
    assert "double-send" in format_message("unconfirmed")
