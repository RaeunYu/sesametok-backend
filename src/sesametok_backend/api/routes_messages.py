"""단일 메시지, 동기화, 발신 엔드포인트."""

from __future__ import annotations

from pathlib import Path

from ..core.send_queue import SendRequest
from ..send.errors import SendError
from .context import ApiContext
from .errors import ApiError, bad_request, not_found
from .router import Request, Response

# 발신 오류 코드별 HTTP 상태. 나머지는 전부 하위 UI 실패다.
SEND_STATUS: dict[str, int] = {
    "bad_request": 400,
    "bad_mode": 400,
    "no_consent": 400,
    "room_name_not_typable": 400,
    "image_path_outside_outbox": 400,
    "room_not_found": 404,
    "target_ambiguous": 409,
    "ambiguous_room": 409,
    "unconfirmed": 409,
    "rate_limited": 429,
    "not_running": 503,
    "accessibility_denied": 503,
}


def _outbox_path(context: ApiContext, raw: object) -> Path:
    """이미 우리 outbox 안에 있는 파일만 보낼 수 있다(경로 탈출 금지)."""

    if not isinstance(raw, str) or not raw.strip():
        raise bad_request("image_path는 문자열이어야 합니다", "image_path_outside_outbox")
    outbox = context.settings.outbox_dir.resolve()
    candidate = Path(raw).expanduser().resolve()
    if outbox not in candidate.parents or not candidate.is_file():
        raise bad_request(
            "image_path는 outbox 디렉터리 안에 있는 기존 파일이어야 합니다 "
            f"({outbox})",
            "image_path_outside_outbox",
        )
    return candidate


def get_message(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    message_id = request.params["message_id"]
    archive = context.open_archive()
    try:
        message = archive.message(message_id)
    finally:
        archive.close()
    if message is None:
        raise not_found(f"아카이브에 {message_id} 메시지가 없습니다", "message_not_found")
    return Response.json(message.to_dict())


def post_sync(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    if context.poller is None:
        raise ApiError(503, "poller_unavailable", "폴러가 실행 중이 아닙니다", retryable=True)
    context.poller.trigger()
    return Response.json({"result": "scheduled"}, status=202)


def post_message(context: ApiContext, request: Request) -> Response:
    """메시지를 발신하거나, 초안을 만들거나, 드라이런한다."""

    context.require_auth(request)
    if context.send_queue is None:
        raise ApiError(
            503,
            "send_unavailable",
            "이 빌드에서는 발신이 활성화되어 있지 않습니다",
            retryable=False,
        )

    payload = request.json()
    image_path = (
        _outbox_path(context, payload.get("image_path"))
        if payload.get("image_path")
        else None
    )
    request_model = SendRequest(
        mode=payload.get("mode", "send") if isinstance(payload.get("mode", "send"), str) else "",
        chat_id=str(payload["chat_id"]) if payload.get("chat_id") is not None else None,
        text=payload.get("text") if isinstance(payload.get("text"), str) else None,
        image_path=image_path,
        background_only=bool(payload.get("background_only", False)),
        accept_use_policy=bool(payload.get("accept_use_policy", False)),
        client_request_id=payload.get("client_request_id")
        if isinstance(payload.get("client_request_id"), str)
        else None,
    )

    try:
        result = context.send_queue.submit(request_model)
    except SendError as exc:
        raise ApiError(
            SEND_STATUS.get(exc.code, 502), exc.code, str(exc), retryable=exc.retryable
        ) from exc

    return Response.json(result.to_dict())
