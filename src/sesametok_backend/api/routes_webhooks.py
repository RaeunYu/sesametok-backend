"""Webhook subscription management (ADR-005).

Targets are restricted to loopback ``http`` on purpose: the backend pushes only
to local interfaces, and reaching a third-party messenger is the interface
project's job.
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
        raise bad_request("kinds must be a non-empty array of event kinds")
    if len(raw) > MAX_KINDS:
        raise bad_request("too many event kinds requested")
    kinds: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in EVENT_KINDS:
            raise bad_request(
                f"unknown event kind {item!r}; valid kinds are {', '.join(DEFAULT_KINDS)}"
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
        raise bad_request("secret must be a string of at least 16 characters")
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

    # The secret is returned exactly once, here.
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
        raise bad_request("body must contain a boolean 'active' field")

    archive = context.open_archive()
    try:
        updated = archive.set_webhook_active(webhook_id, payload["active"])
        archive.commit()
    finally:
        archive.close()
    if not updated:
        raise not_found(f"no webhook {webhook_id}", "webhook_not_found")
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
        raise not_found(f"no webhook {webhook_id}", "webhook_not_found")
    return Response.json({"deleted": True, "id": webhook_id})
