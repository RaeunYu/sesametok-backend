"""방 노출 표시를 바꾸는 **단일 경로**.

API(`PATCH /v1/rooms/{chat_id}`)와 메뉴바가 같은 함수를 호출한다. 그래야 정책이 두 벌이
되지 않고, 두 표면이 같은 이벤트를 만든다. 노출은 표시 범위일 뿐 발신 허가가 아니다.
"""

from __future__ import annotations

from ..models import Room
from .archive import Archive
from .events import EventBus


def set_exposed(
    archive: Archive, bus: EventBus | None, chat_id: str, exposed: bool
) -> Room | None:
    """방의 노출 표시를 바꾸고 ``room.updated`` 이벤트를 발행한다.

    없는 방이면 ``None``을 돌려주고 아무것도 바꾸지 않는다. 같은 값으로 다시 호출해도
    성공하며, 그때도 이벤트를 발행한다(클라이언트가 상태를 즉시 확인할 수 있도록).
    """

    room = archive.set_room_exposed(chat_id, exposed)
    if room is None:
        return None
    event = archive.append_event("room.updated", room.to_dict())
    archive.commit()
    if bus is not None:
        bus.publish(event)
    return room
