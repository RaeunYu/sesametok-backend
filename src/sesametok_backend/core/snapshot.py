"""fingerprint diff — ``message.created``/``message.updated`` 뒤의 순수 로직.

SQLite와 카카오톡에 의존하지 않게 유지하여 규칙을 직접 테스트할 수 있다. 없던
키는 ``created``, fingerprint가 바뀌면 ``updated``, 그 외에는 아무 일도 없다.
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
    """*messages* 를 *existing* 에 저장된 fingerprint와 대조해 분류한다."""

    created: list[Message] = []
    updated: list[Message] = []
    unchanged = 0
    seen: set[ChatMessageKey] = set()

    for message in messages:
        key = message_key(message)
        if key in seen:
            continue  # 리더의 중복 제거 규칙과 동일하게 첫 목격이 이긴다
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
    """이름이나 타입이 아카이브된 값과 다른 방들."""

    changed: list[Room] = []
    seen: set[str] = set()
    for room in rooms:
        if room.chat_id in seen:
            continue
        seen.add(room.chat_id)
        if existing.get(room.chat_id) != room_fingerprint(room):
            changed.append(room)
    return RoomDiff(changed=tuple(changed))
