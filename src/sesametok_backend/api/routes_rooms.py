"""방과 대화 조회 엔드포인트."""

from __future__ import annotations

from ..core.archive import MAX_PAGE_LIMIT
from .context import ApiContext
from .errors import bad_request, not_found
from .router import Request, Response


def list_rooms(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    archive = context.open_archive()
    try:
        rooms = [room.to_dict() for room in archive.rooms()]
    finally:
        archive.close()
    return Response.json({"rooms": rooms})


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
