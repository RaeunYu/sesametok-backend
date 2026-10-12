"""루프백 API의 라우트 테이블."""

from __future__ import annotations

from .context import ApiContext
from .router import Router
from .routes_events import get_events
from .routes_messages import get_message, post_message, post_sync
from .routes_rooms import get_room, get_room_messages, list_rooms
from .routes_status import get_health, get_status
from .routes_webhooks import (
    create_webhook,
    delete_webhook,
    list_webhooks,
    patch_webhook,
)


def build_router(context: ApiContext | None = None) -> Router:
    router = Router()
    router.add("GET", "/health", get_health)
    router.add("GET", "/v1/status", get_status)

    router.add("GET", "/v1/rooms", list_rooms)
    router.add("GET", "/v1/rooms/{chat_id}", get_room)
    router.add("GET", "/v1/rooms/{chat_id}/messages", get_room_messages)

    router.add("GET", "/v1/messages/{message_id}", get_message)
    router.add("POST", "/v1/messages", post_message)
    router.add("POST", "/v1/sync", post_sync)

    router.add("GET", "/v1/events", get_events)

    router.add("GET", "/v1/webhooks", list_webhooks)
    router.add("POST", "/v1/webhooks", create_webhook)
    router.add("PATCH", "/v1/webhooks/{webhook_id}", patch_webhook)
    router.add("DELETE", "/v1/webhooks/{webhook_id}", delete_webhook)
    return router
