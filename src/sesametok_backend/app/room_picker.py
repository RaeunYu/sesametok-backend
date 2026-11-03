"""노출 방 선택 창의 **순수 로직**(AppKit 없이 테스트 가능).

UI는 이 함수들만 호출한다. 검색 필터·변경분 계산·요약 문자열을 여기서 고정해 두면
메뉴바를 띄우지 않고도 동작을 검증할 수 있다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

MENU_TITLE = "노출 방 관리…"


def summary(exposed: int, total: int) -> str:
    """메뉴 항목 제목. 예: ``노출 방 관리… (7/411)``."""

    return f"{MENU_TITLE} ({exposed}/{total})"


def _sort_key(room: Mapping[str, Any]) -> tuple[int, str, str]:
    """최근 메시지 시각 내림차순. 시각이 없는 방은 뒤로 보낸다."""

    last = room.get("last_message_at")
    return (0 if last else 1, _reverse(last), str(room.get("chat_id", "")))


def _reverse(value: Any) -> str:
    """문자열 오름차순으로 내림차순을 흉내 낸다(ASCII 반전)."""

    text = str(value or "")
    return "".join(chr(0x10FFFF - ord(ch)) for ch in text)


def filtered_rooms(
    rooms: Iterable[Mapping[str, Any]], query: str = ""
) -> list[Mapping[str, Any]]:
    """방 이름 부분 일치(대소문자 무시)로 거르고 최근 순으로 정렬한다."""

    needle = query.strip().casefold()
    selected = [
        room
        for room in rooms
        if not needle or needle in str(room.get("chat_name", "")).casefold()
    ]
    return sorted(selected, key=_sort_key)


def changed_rooms(
    initial: Mapping[str, bool], selected: Mapping[str, bool]
) -> list[tuple[str, bool]]:
    """처음 상태와 달라진 ``(chat_id, exposed)``만 돌려준다."""

    changes: list[tuple[str, bool]] = []
    for chat_id, value in selected.items():
        if bool(initial.get(chat_id, False)) != bool(value):
            changes.append((str(chat_id), bool(value)))
    return sorted(changes)


def counts(rooms: Iterable[Mapping[str, Any]], selected: Mapping[str, bool]) -> tuple[int, int]:
    """(선택된 방 수, 전체 방 수)."""

    items = list(rooms)
    return sum(1 for value in selected.values() if value), len(items)
