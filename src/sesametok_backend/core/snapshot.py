"""Fingerprint diffing — the pure logic behind ``message.created``/``message.updated``.

Kept free of SQLite and KakaoTalk so the rules can be tested directly: a key
that was absent becomes ``created``, a changed fingerprint becomes ``updated``,
everything else is silent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..models import Message, Room

ChatMessageKey = tuple[str, str]


@dataclass(frozen=True)
class MessageDiff:
    created: tuple[Message, ...] = ()
    updated: tuple[Message, ...] = ()
    unchanged: int = 0

    @property
    def total_changes(self) -> int:
        return len(self.created) + len(self.updated)


@dataclass(frozen=True)
class RoomDiff:
    changed: tuple[Room, ...] = field(default=())


def message_key(message: Message) -> ChatMessageKey:
    return (message.chat_id, message.message_id)


def diff_messages(
    existing: Mapping[ChatMessageKey, tuple],
    messages: Sequence[Message],
) -> MessageDiff:
    """Classify *messages* against the stored fingerprints in *existing*."""

    created: list[Message] = []
    updated: list[Message] = []
    unchanged = 0
    seen: set[ChatMessageKey] = set()

    for message in messages:
        key = message_key(message)
        if key in seen:
            continue  # first sighting wins, matching the reader's dedupe rule
        seen.add(key)

        previous = existing.get(key)
        if previous is None:
            created.append(message)
        elif tuple(previous) != message.fingerprint():
            updated.append(message)
        else:
            unchanged += 1

    return MessageDiff(created=tuple(created), updated=tuple(updated), unchanged=unchanged)


def room_fingerprint(room: Room) -> tuple[str, str]:
    return (room.chat_name, room.chat_type)


def diff_rooms(existing: Mapping[str, tuple], rooms: Sequence[Room]) -> RoomDiff:
    """Rooms whose name or type differs from the archived value."""

    changed: list[Room] = []
    seen: set[str] = set()
    for room in rooms:
        if room.chat_id in seen:
            continue
        seen.add(room.chat_id)
        if existing.get(room.chat_id) != room_fingerprint(room):
            changed.append(room)
    return RoomDiff(changed=tuple(changed))
