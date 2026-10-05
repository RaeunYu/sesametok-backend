"""Resumable SSE event stream.

A client that sends ``Last-Event-ID``/``?since=`` gets the archive replayed from
that sequence. A client that sends nothing starts at the current head, so a fresh
interface is not forced to replay a large backlog it will fetch over REST anyway.

The subscription is created *before* replay, so no event can slip through the gap
between reading history and going live.
"""

from __future__ import annotations

from ..models import Event
from .context import ApiContext
from .errors import bad_request, conflict
from .router import Request, Response
from .sse import EVENT_STREAM_CONTENT_TYPE, KEEP_ALIVE_COMMENT, render_event

REPLAY_BATCH = 500
KEEP_ALIVE_SECONDS = 15


def _parse_since(raw: str | None) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise bad_request("since/Last-Event-ID must be an integer sequence") from exc
    if value < 0:
        raise bad_request("since/Last-Event-ID must not be negative")
    return value


def get_events(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    since = _parse_since(request.query_one("since") or request.header("Last-Event-ID"))

    if since is not None:
        archive = context.open_archive()
        try:
            oldest = archive.oldest_seq()
            if oldest > 0 and since + 1 < oldest:
                raise conflict(
                    f"events after seq {since} are no longer retained (oldest is {oldest}); "
                    "backfill history with GET /v1/rooms/{chat_id}/messages",
                    "event_replay_unavailable",
                )
        finally:
            archive.close()

    subscription = context.bus.subscribe()

    def stream(out) -> None:
        last = since
        try:
            archive = context.open_archive()
            try:
                if last is None:
                    last = archive.latest_seq()
                while True:
                    batch = archive.events_since(last, limit=REPLAY_BATCH)
                    if not batch:
                        break
                    for event in batch:
                        out.write(render_event(event).encode("utf-8"))
                        last = event.seq
                    out.flush()
            finally:
                archive.close()

            # Anything queued while replaying that we have not already sent.
            for event in subscription.drain():
                if event.seq > last:
                    out.write(render_event(event).encode("utf-8"))
                    last = event.seq
            out.flush()

            while not context.shutdown.is_set():
                event: Event | None = subscription.get(timeout=KEEP_ALIVE_SECONDS)
                if event is None:
                    out.write(KEEP_ALIVE_COMMENT.encode("utf-8"))
                    out.flush()
                    continue
                if event.seq <= last:
                    continue
                out.write(render_event(event).encode("utf-8"))
                out.flush()
                last = event.seq
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            context.bus.unsubscribe(subscription)

    return Response(status=200, stream=stream, headers={"Content-Type": EVENT_STREAM_CONTENT_TYPE})
