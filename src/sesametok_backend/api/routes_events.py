"""이어받을 수 있는 SSE 이벤트 스트림.

``Last-Event-ID``/``?since=``를 보낸 클라이언트는 그 시퀀스부터 아카이브를 재생해서 받는다.
아무것도 보내지 않은 클라이언트는 현재 헤드에서 시작한다. 그래야 새로 뜬 인터페이스가
어차피 REST로 가져올 방대한 백로그를 재생하도록 강제되지 않는다.

구독은 재생 *전에* 만든다. 그래야 히스토리를 읽는 시점과 라이브로 전환하는 시점 사이의
틈으로 이벤트가 빠져나갈 수 없다.
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
        raise bad_request("since/Last-Event-ID는 정수 시퀀스여야 합니다") from exc
    if value < 0:
        raise bad_request("since/Last-Event-ID는 음수일 수 없습니다")
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
                    f"seq {since} 이후 이벤트는 더 이상 보관되지 않습니다(가장 오래된 것은 {oldest}); "
                    "GET /v1/rooms/{chat_id}/messages로 히스토리를 채우세요",
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

            # 재생하는 동안 큐에 쌓였지만 아직 보내지 않은 것들.
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
