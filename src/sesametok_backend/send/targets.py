"""아카이브의 방을 카카오톡 UI로 구동할 수 있는 대상으로 바꾼다.

AX 계층은 창 제목과 채팅 목록 행만 알 뿐, chat id는 모른다. 따라서 chat id를
*이름 + 순위*로 환원해야 한다. 순위는 같은 이름을 가진 채팅들 사이에서의
0부터 시작하는 위치이며, 최신순으로 정렬한다. 이것이 모호하면 추측하지 않고
전송을 거부한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Room

# 우리가 입력하려는 채팅 이름에는 절대 나타날 수 없는 문자들.
# 제어 문자와 보이지 않는/서식 문자 범위.
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
    """아카이브의 방을 화면 위의 유일한 대상으로 환원할 수 없다."""

    def __init__(self, code: str, **details: object) -> None:
        self.code = code
        self.details = details
        super().__init__(code)


def room_member_key(title: str) -> list[str]:
    """단체방 제목을 구성원 이름들로 나눈다. 순서는 무시한다."""

    return sorted(part.strip() for part in title.split(",") if part.strip())


def room_matches(candidate: str, wanted: str) -> bool:
    """정확히 일치하거나, 같은 구성원 집합이 다른 순서로 있어도 일치한다."""

    if candidate == wanted:
        return True
    key_candidate = room_member_key(candidate)
    return bool(key_candidate) and key_candidate == room_member_key(wanted)


def search_query(title: str) -> str:
    """채팅 검색에 입력할 문자열: 가장 긴 구성원 이름, 없으면 제목 전체."""

    members = room_member_key(title)
    if not members:
        return title
    longest = max(len(member) for member in members)
    return next(member for member in members if len(member) == longest)


def untypable_characters(name: str) -> list[str]:
    """*name* 안에서 채팅 이름이 담을 수 없는 코드포인트들(``U+XXXX`` 형식)."""

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
    """같은 이름의 방들을 최신순으로 정렬한다. ``None``은 마지막, chat_id 동률은 오름차순."""

    ordered = sorted(rooms, key=lambda room: room.chat_id)
    ordered.sort(key=lambda room: room.last_message_at or "", reverse=True)
    ordered.sort(key=lambda room: room.last_message_at is None)
    return ordered


def resolve_target(rooms: list[Room], chat_id: str) -> SendTarget:
    """*chat_id*를 ``(이름, 순위)``로 환원하거나 ``TargetError``를 발생시킨다.

    같은 이름의 방들 순서를 정할 수 없으면 ``target_ambiguous``를 발생시킨다.
    잘못된 행을 클릭하면 되돌릴 수 없기 때문이다.
    """

    room = next((candidate for candidate in rooms if candidate.chat_id == chat_id), None)
    if room is None:
        raise TargetError("room_not_found", room=chat_id)

    validate_room_name(room.chat_name)

    same = [candidate for candidate in rooms if room_matches(candidate.chat_name, room.chat_name)]
    if len(same) <= 1:
        return SendTarget(chat_id, room.chat_name, 0, room.chat_type)

    # 최근성이 같은 두 방은 화면에서 구분할 수 없다.
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
