"""웹훅 구독 관리 (ADR-005).

대상은 일부러 루프백 ``http``로 제한한다. 이 백엔드는 로컬 인터페이스로만 밀어 넣으며,
서드파티 메신저에 도달하는 일은 인터페이스 프로젝트의 몫이다.
"""

from __future__ import annotations

import secrets
import uuid
from urllib.parse import urlsplit

from ..models import EVENT_KINDS
from .context import ApiContext
from .errors import bad_request, not_found
from .router import Request, Response
from .webhooks import LOOPBACK_HOSTS, validate_webhook_url

DEFAULT_KINDS = tuple(sorted(EVENT_KINDS))
MAX_KINDS = 16


def _parse_kinds(raw: object) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_KINDS
    if not isinstance(raw, list) or not raw:
        raise bad_request("kinds는 비어 있지 않은 이벤트 종류 배열이어야 합니다")
    if len(raw) > MAX_KINDS:
        raise bad_request("요청한 이벤트 종류가 너무 많습니다")
    kinds: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in EVENT_KINDS:
            raise bad_request(
                f"알 수 없는 이벤트 종류 {item!r}; 유효한 종류는 {', '.join(DEFAULT_KINDS)}입니다"
            )
        kinds.append(item)
    return tuple(dict.fromkeys(kinds))


def list_webhooks(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    archive = context.open_archive()
    try:
        webhooks = archive.list_webhooks(include_secret=False)
    finally:
        archive.close()
    return Response.json({"webhooks": webhooks})


def create_webhook(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    payload = request.json()
    url = validate_webhook_url(payload.get("url"))
    kinds = _parse_kinds(payload.get("kinds"))
    secret = payload.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) < 16):
        raise bad_request("secret은 16자 이상의 문자열이어야 합니다")
    secret = secret or secrets.token_urlsafe(32)
    webhook_id = str(uuid.uuid4())

    archive = context.open_archive()
    try:
        record = archive.add_webhook(
            webhook_id=webhook_id, url=url, secret=secret, kinds=kinds
        )
        archive.commit()
    finally:
        archive.close()

    # secret은 여기서 딱 한 번만 반환된다.
    return Response.json(
        {
            "id": record["id"],
            "url": record["url"],
            "kinds": record["kinds"],
            "active": record["active"],
            "cursor_seq": record["cursor_seq"],
            "secret": secret,
            "signature_header": "X-Sesametok-Signature: sha256=<hmac-sha256(secret, body)>",
        },
        status=201,
    )


def patch_webhook(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    webhook_id = request.params["webhook_id"]
    payload = request.json()
    if "active" not in payload or not isinstance(payload["active"], bool):
        raise bad_request("본문에 불리언 'active' 필드가 있어야 합니다")

    archive = context.open_archive()
    try:
        updated = archive.set_webhook_active(webhook_id, payload["active"])
        archive.commit()
    finally:
        archive.close()
    if not updated:
        raise not_found(f"해당 웹훅이 없습니다: {webhook_id}", "webhook_not_found")
    return Response.json({"id": webhook_id, "active": payload["active"]})


def delete_webhook(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    webhook_id = request.params["webhook_id"]
    archive = context.open_archive()
    try:
        deleted = archive.delete_webhook(webhook_id)
        archive.commit()
    finally:
        archive.close()
    if not deleted:
        raise not_found(f"해당 웹훅이 없습니다: {webhook_id}", "webhook_not_found")
    return Response.json({"deleted": True, "id": webhook_id})
