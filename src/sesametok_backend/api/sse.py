"""Server-Sent Events rendering."""

from __future__ import annotations

import json

from ..models import Event

EVENT_STREAM_CONTENT_TYPE = "text/event-stream"
KEEP_ALIVE_COMMENT = ": ping\n\n"


def render_event(event: Event) -> str:
    payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {payload}\n\n"


def render_error(code: str, message: str) -> str:
    payload = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
    return f"event: error\ndata: {payload}\n\n"
