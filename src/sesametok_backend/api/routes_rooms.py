"""방과 대화 조회 엔드포인트."""

from __future__ import annotations

from ..core.archive import MAX_PAGE_LIMIT
from ..core.rooms import set_exposed
from .context import ApiContext
from .errors import bad_request, not_found
from .router import Request, Response


def _parse_exposed_filter(raw: str | None) -> bool:
    """``exposed`` 쿼리: 없으면 전체, ``1``/``true``면 노출된 방만. 그 밖은 400."""

    if raw is None:
        return False
    if raw in ("1", "true"):
        return True
    raise bad_request("exposed는 1 또는 true만 허용합니다", "bad_request")


def list_rooms(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    exposed_only = _parse_exposed_filter(request.query_one("exposed"))
    archive = context.open_archive()
    try:
        rooms = [room.to_dict() for room in archive.rooms(exposed_only=exposed_only)]
    finally:
        archive.close()
    return Response.json({"rooms": rooms})


def patch_room(context: ApiContext, request: Request) -> Response:
    """방의 노출 표시를 바꾼다. 멱등이며, 성공하면 ``room.updated``를 발행한다."""

    context.require_auth(request)
    chat_id = request.params["chat_id"]
    payload = request.json()
    exposed = payload.get("exposed")
    if not isinstance(exposed, bool):
        raise bad_request("exposed는 JSON boolean이어야 합니다", "bad_request")

    archive = context.open_archive()
    try:
        room = set_exposed(archive, context.bus, chat_id, exposed)
    finally:
        archive.close()
    if room is None:
        raise not_found(f"chat_id={chat_id}인 아카이브 방이 없습니다", "room_not_found")
    return Response.json(room.to_dict())


def get_room(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    chat_id = request.params["chat_id"]
    archive = context.open_archive()
    try:
        room = archive.room(chat_id)
    finally:
        archive.close()
    if room is None:
        raise not_found(f"chat_id={chat_id}인 아카이브 방이 없습니다", "room_not_found")
    return Response.json(room.to_dict())


def _parse_limit(raw: str | None) -> int:
    if raw is None:
        return 100
    try:
        value = int(raw)
    except ValueError as exc:
        raise bad_request("limit은 정수여야 합니다") from exc
    if value < 1:
        raise bad_request("limit은 1 이상이어야 합니다")
    return min(value, MAX_PAGE_LIMIT)


def get_room_messages(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    chat_id = request.params["chat_id"]
    archive = context.open_archive()
    try:
        page = archive.messages(
            chat_id,
            since=request.query_one("since"),
            until=request.query_one("until"),
            limit=_parse_limit(request.query_one("limit")),
            before=request.query_one("before"),
        )
    finally:
        archive.close()
    return Response.json(
        {
            "chat_id": chat_id,
            "messages": [message.to_dict() for message in page.messages],
            "next_before": page.next_before,
            "has_more": page.has_more,
        }
    )
