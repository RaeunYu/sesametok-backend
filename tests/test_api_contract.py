"""API 계약 테스트.

합성 암호화 아카이브를 두고 실제 HTTP 서버를 루프백으로 돌린다. 카카오톡 설치나 실제
네트워크는 관여하지 않는다.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from sesametok_backend.api.context import ApiContext
from sesametok_backend.api.routes import build_router
from sesametok_backend.api.server import ApiService
from sesametok_backend.config import Settings
from sesametok_backend.core.archive import Archive
from sesametok_backend.core.events import EventBus
from sesametok_backend.core.poller import PollStats
from sesametok_backend.models import Message, Room

KEY = bytes.fromhex("ab" * 32)
ACCOUNT = "account-hash"
TOKEN = "unit-test-token"


class FakePoller:
    def __init__(self) -> None:
        self.stats = PollStats()
        self.triggered = 0
        self.paused = False
        self.stopping = False

    def trigger(self) -> None:
        self.triggered += 1


class FakeSendQueue:
    """요청을 기록하고 실제 큐의 동의 게이트를 흉내 낸다."""

    def __init__(self) -> None:
        self.requests: list = []

    def submit(self, request, *, timeout: float | None = None):
        from sesametok_backend.core.send_queue import SendResult
        from sesametok_backend.send.errors import SendError

        self.requests.append(request)
        if request.mode in ("send", "draft") and not request.accept_use_policy:
            raise SendError("no_consent")
        return SendResult(
            result="dry_run" if request.mode == "dry_run" else "sent",
            room="direct room",
            chars=len(request.text or ""),
            client_request_id=request.client_request_id,
        )


def _message(message_id: str, *, chat_id: str = "100", timestamp: str, text: str = "hello"):
    return Message(
        message_id=message_id,
        chat_id=chat_id,
        chat_name="room",
        chat_type="direct",
        sender_id="500",
        sender_nickname="nick",
        timestamp=timestamp,
        text=text,
        message_type="text",
    )


@pytest.fixture
def api(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        host="127.0.0.1",
        port=0,
        # pytest의 tmp 디렉터리는 macOS의 AF_UNIX 경로로 쓰기에는 너무 길다.
        uds_path_override=Path("/tmp") / f"st-{tmp_path.name}.sock",
    )
    settings.ensure_directories()

    archive = Archive.open(settings, KEY, account_hash=ACCOUNT)
    archive.upsert_rooms(
        [
            Room("100", "direct room", "direct", "2026-01-01T00:02:00+00:00"),
            Room("200", "group room", "group", "2026-01-01T00:00:00+00:00"),
        ]
    )
    archive.apply_message_diff(
        [
            _message("100-1", timestamp="2026-01-01T00:00:00+00:00"),
            _message("100-2", timestamp="2026-01-01T00:01:00+00:00"),
            _message("100-3", timestamp="2026-01-01T00:02:00+00:00"),
            _message("200-1", chat_id="200", timestamp="2026-01-01T00:00:00+00:00"),
        ]
    )
    archive.commit()
    archive.close()

    bus = EventBus()
    poller = FakePoller()
    context = ApiContext(
        settings=settings,
        token=TOKEN,
        archive_factory=lambda: Archive.open(
            settings, KEY, account_hash=ACCOUNT, initialize=False
        ),
        bus=bus,
        poller=poller,  # type: ignore[arg-type]
        send_queue=FakeSendQueue(),  # type: ignore[arg-type]
        health_provider=lambda: {
            "running": True,
            "kakaotalk_running": True,
            "permissions_ok": True,
            "polling": True,
            "last_sync_at": "2026-01-01T00:00:05+00:00",
        },
        status_provider=lambda: {"version": "0.1.0", "account": {"source": "test"}},
        webhook_count=lambda: {"total": 0, "active": 0},
    )
    service = ApiService(
        context,
        build_router(context),
        host="127.0.0.1",
        port=0,
        uds_path=settings.uds_path,
    )
    service.start()
    try:
        yield service, context, settings
    finally:
        service.stop()


def raw_http(
    service: ApiService,
    method: str,
    path: str,
    *,
    token: str | None = TOKEN,
    body: dict | None = None,
    timeout: float = 3.0,
) -> tuple[int, bytes]:
    """SSE 스트림이 멈추지 않고 읽히도록 만든 최소한의 HTTP/1.1 클라이언트."""

    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
    if token is not None:
        headers.append(f"Authorization: Bearer {token}")
    if body is not None:
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(payload)}")

    with socket.create_connection((service.host, service.port), timeout=timeout) as sock:
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + payload)
        chunks: list[bytes] = []
        while True:
            try:
                part = sock.recv(65536)
            except socket.timeout:
                break
            if not part:
                break
            chunks.append(part)

    raw = b"".join(chunks)
    head, _, response_body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, response_body


def call(service: ApiService, method: str, path: str, **kwargs):
    status, body = raw_http(service, method, path, **kwargs)
    payload = json.loads(body) if body else None
    return status, payload


def test_health_needs_no_token(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/health", token=None)

    assert status == 200
    assert payload["ok"] is True
    assert payload["kakaotalk_running"] is True


def test_v1_requires_a_token(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/v1/rooms", token=None)

    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_wrong_token_is_rejected(api) -> None:
    service, _, _ = api
    status, _ = call(service, "GET", "/v1/rooms", token="nope")
    assert status == 401


def test_rooms_list(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/v1/rooms")

    assert status == 200
    assert [room["chat_id"] for room in payload["rooms"]] == ["100", "200"]
    assert payload["rooms"][0]["message_count"] == 3


def test_room_lookup_and_missing(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/v1/rooms/100")
    assert status == 200
    assert payload["chat_name"] == "direct room"

    status, payload = call(service, "GET", "/v1/rooms/999")
    assert status == 404
    assert payload["error"]["code"] == "room_not_found"


def test_messages_pagination(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/v1/rooms/100/messages?limit=2")

    assert status == 200
    assert [m["message_id"] for m in payload["messages"]] == ["100-2", "100-3"]
    assert payload["has_more"] is True
    assert payload["next_before"] == "100-2"

    status, older = call(
        service, "GET", f"/v1/rooms/100/messages?limit=2&before={payload['next_before']}"
    )
    assert [m["message_id"] for m in older["messages"]] == ["100-1"]
    assert older["has_more"] is False


def test_messages_rejects_bad_limit(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/v1/rooms/100/messages?limit=zero")

    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_single_message(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/v1/messages/100-1")
    assert status == 200
    assert payload["message_id"] == "100-1"

    status, payload = call(service, "GET", "/v1/messages/100-404")
    assert status == 404
    assert payload["error"]["code"] == "message_not_found"


def test_status_reports_archive_counts(api) -> None:
    service, _, _ = api
    status, payload = call(service, "GET", "/v1/status")

    assert status == 200
    assert payload["archive"]["rooms"] == 2
    assert payload["archive"]["messages"] == 4
    assert payload["version"] == "0.1.0"


def test_sync_triggers_the_poller(api) -> None:
    service, context, _ = api
    status, payload = call(service, "POST", "/v1/sync")

    assert status == 202
    assert payload["result"] == "scheduled"
    assert context.poller.triggered == 1


def test_dry_run_resolves_the_target(api) -> None:
    service, _, _ = api
    status, payload = call(
        service, "POST", "/v1/messages", body={"chat_id": "100", "text": "hi", "mode": "dry_run"}
    )

    assert status == 200
    assert payload["result"] == "dry_run"
    assert payload["room"] == "direct room"


def test_send_without_consent_is_refused(api) -> None:
    service, _, _ = api
    status, payload = call(
        service, "POST", "/v1/messages", body={"chat_id": "100", "text": "hi", "mode": "send"}
    )

    assert status == 400
    assert payload["error"]["code"] == "no_consent"


def test_send_with_consent_succeeds(api) -> None:
    service, context, _ = api
    status, payload = call(
        service,
        "POST",
        "/v1/messages",
        body={
            "chat_id": "100",
            "text": "hi",
            "mode": "send",
            "accept_use_policy": True,
            "client_request_id": "abc",
        },
    )

    assert status == 200
    assert payload["result"] == "sent"
    assert payload["client_request_id"] == "abc"
    assert context.send_queue.requests[-1].text == "hi"


def test_image_path_must_live_in_the_outbox(api) -> None:
    service, _, settings = api
    settings.ensure_directories()

    status, payload = call(
        service,
        "POST",
        "/v1/messages",
        body={
            "chat_id": "100",
            "mode": "send",
            "accept_use_policy": True,
            "image_path": "/etc/hosts",
        },
    )

    assert status == 400
    assert payload["error"]["code"] == "image_path_outside_outbox"

    inside = settings.outbox_dir / "picture.png"
    inside.write_bytes(b"\x89PNG")
    status, payload = call(
        service,
        "POST",
        "/v1/messages",
        body={
            "chat_id": "100",
            "mode": "send",
            "accept_use_policy": True,
            "image_path": str(inside),
        },
    )
    assert status == 200
    assert payload["result"] == "sent"


def test_unknown_route_and_method(api) -> None:
    service, _, _ = api
    assert call(service, "GET", "/v1/nope")[0] == 404
    assert call(service, "PUT", "/v1/rooms")[0] == 405


def test_sse_replays_from_since(api) -> None:
    service, context, settings = api
    archive = context.open_archive()
    try:
        first = archive.append_event("message.created", {"message_id": "100-1"})
        second = archive.append_event("message.updated", {"message_id": "100-1"})
        archive.commit()
    finally:
        archive.close()

    status, body = raw_http(
        service, "GET", f"/v1/events?since={first.seq - 1}", timeout=1.5
    )
    text = body.decode("utf-8", errors="replace")

    assert status == 200
    assert f"id: {first.seq}" in text
    assert f"id: {second.seq}" in text
    assert "event: message.created" in text
    assert "event: message.updated" in text


def test_sse_rejects_a_replayed_away_position(api) -> None:
    service, context, _ = api
    archive = context.open_archive()
    try:
        for _ in range(5):
            archive.append_event("message.created", {})
        archive.commit()
        archive.prune_events(days=30, max_count=2)
    finally:
        archive.close()

    status, payload = call(service, "GET", "/v1/events?since=1")
    assert status == 409
    assert payload["error"]["code"] == "event_replay_unavailable"


def test_unix_socket_serves_the_same_api(api) -> None:
    service, _, settings = api
    request = (
        f"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n".encode("utf-8")
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3.0)
        sock.connect(str(settings.uds_path))
        sock.sendall(request)
        chunks: list[bytes] = []
        while True:
            try:
                part = sock.recv(65536)
            except socket.timeout:
                break
            if not part:
                break
            chunks.append(part)

    raw = b"".join(chunks)
    assert b"200" in raw.split(b"\r\n", 1)[0]
    assert b'"ok"' in raw
    # 소켓은 다른 사용자가 읽거나 쓸 수 있으면 안 된다.
    assert settings.uds_path.stat().st_mode & 0o077 == 0


def test_webhook_crud_and_secret_handling(api) -> None:
    service, _, _ = api

    status, created = call(
        service,
        "POST",
        "/v1/webhooks",
        body={"url": "http://127.0.0.1:9999/hook", "kinds": ["message.created"]},
    )
    assert status == 201
    assert created["secret"]
    assert created["kinds"] == ["message.created"]
    webhook_id = created["id"]

    status, listed = call(service, "GET", "/v1/webhooks")
    assert status == 200
    assert len(listed["webhooks"]) == 1
    assert "secret" not in listed["webhooks"][0]

    status, patched = call(
        service, "PATCH", f"/v1/webhooks/{webhook_id}", body={"active": False}
    )
    assert status == 200 and patched["active"] is False

    status, deleted = call(service, "DELETE", f"/v1/webhooks/{webhook_id}")
    assert status == 200 and deleted["deleted"] is True
    assert call(service, "DELETE", f"/v1/webhooks/{webhook_id}")[0] == 404


def test_webhook_rejects_external_and_non_http_targets(api) -> None:
    service, _, _ = api

    status, payload = call(
        service, "POST", "/v1/webhooks", body={"url": "http://example.com/hook"}
    )
    assert status == 400
    assert "루프백" in payload["error"]["message"]

    status, payload = call(
        service, "POST", "/v1/webhooks", body={"url": "https://127.0.0.1:9999/hook"}
    )
    assert status == 400

    status, payload = call(
        service, "POST", "/v1/webhooks", body={"url": "http://127.0.0.1/hook"}
    )
    assert status == 400
    assert "포트" in payload["error"]["message"]


def test_webhook_rejects_unknown_kinds(api) -> None:
    service, _, _ = api
    status, payload = call(
        service,
        "POST",
        "/v1/webhooks",
        body={"url": "http://127.0.0.1:9999/hook", "kinds": ["not.a.kind"]},
    )
    assert status == 400
    assert "알 수 없는 이벤트 종류" in payload["error"]["message"]


def test_closed_room_maps_to_502() -> None:
    """창이 열려 있지 않으면 502와 명시적 코드를 돌려준다."""

    from sesametok_backend.api.routes_messages import SEND_STATUS

    assert SEND_STATUS["window_not_open"] == 502
