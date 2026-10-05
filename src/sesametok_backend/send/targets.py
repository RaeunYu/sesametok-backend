"""Turning an archive room into something the KakaoTalk UI can be driven to.

The AX layer only knows window titles and chat-list rows, never chat ids
A chat id therefore has to be reduced to a
*name plus rank*: the 0-based position among chats that share that name, ordered
newest-first. When that is ambiguous the send is refused rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Room

# Characters that can never appear in a chat name we are willing to type.
# Control characters plus the invisible/format ranges.
UNSAFE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x001F),
    (0x007F, 0x009F),
    (0x00AD, 0x00AD),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
)


class TargetError(ValueError):
    """The archived room cannot be reduced to a unique on-screen target."""

    def __init__(self, code: str, **details: object) -> None:
        self.code = code
        self.details = details
        super().__init__(code)


def room_member_key(title: str) -> list[str]:
    """Split a group-room title into its member names, order-insensitively."""

    return sorted(part.strip() for part in title.split(",") if part.strip())


def room_matches(candidate: str, wanted: str) -> bool:
    """Exact match, or the same member set in a different order."""

    if candidate == wanted:
        return True
    key_candidate = room_member_key(candidate)
    return bool(key_candidate) and key_candidate == room_member_key(wanted)


def search_query(title: str) -> str:
    """The string to type into chat search: the longest member, else the title."""

    members = room_member_key(title)
    if not members:
        return title
    longest = max(len(member) for member in members)
    return next(member for member in members if len(member) == longest)


def untypable_characters(name: str) -> list[str]:
    """Codepoints in *name* that a chat name cannot contain (as ``U+XXXX``)."""

    offenders: list[str] = []
    for character in name:
        codepoint = ord(character)
        if any(low <= codepoint <= high for low, high in UNSAFE_RANGES):
            label = f"U+{codepoint:04X}"
            if label not in offenders:
                offenders.append(label)
    return offenders


def validate_room_name(name: str) -> None:
    offenders = untypable_characters(name)
    if offenders:
        raise TargetError("room_name_not_typable", offenders=", ".join(offenders))


@dataclass(frozen=True)
class SendTarget:
    chat_id: str
    room_name: str
    rank: int
    chat_type: str


def _newest_first(rooms: list[Room]) -> list[Room]:
    """Order same-named rooms newest-first, ``None`` last, chat_id ties ascending."""

    ordered = sorted(rooms, key=lambda room: room.chat_id)
    ordered.sort(key=lambda room: room.last_message_at or "", reverse=True)
    ordered.sort(key=lambda room: room.last_message_at is None)
    return ordered


def resolve_target(rooms: list[Room], chat_id: str) -> SendTarget:
    """Reduce *chat_id* to ``(name, rank)`` or raise ``TargetError``.

    Raises ``target_ambiguous`` when same-named rooms cannot be ordered, because
    clicking the wrong row cannot be undone.
    """

    room = next((candidate for candidate in rooms if candidate.chat_id == chat_id), None)
    if room is None:
        raise TargetError("room_not_found", room=chat_id)

    validate_room_name(room.chat_name)

    same = [candidate for candidate in rooms if room_matches(candidate.chat_name, room.chat_name)]
    if len(same) <= 1:
        return SendTarget(chat_id, room.chat_name, 0, room.chat_type)

    # Two rooms with the same recency cannot be told apart on screen.
    stamp = room.last_message_at
    if any(
        candidate.chat_id != chat_id and candidate.last_message_at == stamp
        for candidate in same
    ):
        raise TargetError("target_ambiguous", room=room.chat_name)

    ordered = _newest_first(same)
    position = next(
        index for index, candidate in enumerate(ordered) if candidate.chat_id == chat_id
    )
    return SendTarget(chat_id, room.chat_name, position, room.chat_type)
