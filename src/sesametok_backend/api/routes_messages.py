"""Single-message, sync, and send endpoints."""

from __future__ import annotations

from pathlib import Path

from ..core.send_queue import SendRequest
from ..send.errors import SendError
from .context import ApiContext
from .errors import ApiError, bad_request, not_found
from .router import Request, Response

# HTTP status per send error code. Everything else is a downstream UI failure.
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
    """Only files already inside our outbox may be sent (no path traversal)."""

    if not isinstance(raw, str) or not raw.strip():
        raise bad_request("image_path must be a string", "image_path_outside_outbox")
    outbox = context.settings.outbox_dir.resolve()
    candidate = Path(raw).expanduser().resolve()
    if outbox not in candidate.parents or not candidate.is_file():
        raise bad_request(
            "image_path must be an existing file inside the outbox directory "
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
        raise not_found(f"no archived message {message_id}", "message_not_found")
    return Response.json(message.to_dict())


def post_sync(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    if context.poller is None:
        raise ApiError(503, "poller_unavailable", "the poller is not running", retryable=True)
    context.poller.trigger()
    return Response.json({"result": "scheduled"}, status=202)


def post_message(context: ApiContext, request: Request) -> Response:
    """Send, draft, or dry-run a message."""

    context.require_auth(request)
    if context.send_queue is None:
        raise ApiError(
            503,
            "send_unavailable",
            "sending is not enabled in this build",
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
