"""Integration: the real poller/archive behind the real HTTP API.

No KakaoTalk involved — the source is synthetic — but the poller, the encrypted
archive, the event bus, and the HTTP server are the production objects.
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
from sesametok_backend.core.poller import Poller
from sesametok_backend.kakao.reader import Snapshot
from sesametok_backend.models import Message, Room

KEY = bytes.fromhex("ab" * 32)
ACCOUNT = "account-hash"
TOKEN = "integration-token"
NOW = 1_767_225_600


class ScriptedSource:
    def __init__(self, snapshots) -> None:
        self._snapshots = list(snapshots)

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        return self._snapshots.pop(0)


def _message(message_id: str, *, timestamp: str, text: str = "hello") -> Message:
    return Message(
        message_id=message_id,
        chat_id="100",
        chat_name="Integration Room",
        chat_type="direct",
        sender_id="500",
        sender_nickname="nick",
        timestamp=timestamp,
        text=text,
        message_type="text",
    )


def _snapshot(messages=(), rooms=()) -> Snapshot:
    return Snapshot(
        rooms=tuple(rooms), messages=tuple(messages), skipped_rows=0, open_chat_ids=frozenset()
    )


@pytest.fixture
def service(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        host="127.0.0.1",
        port=0,
        uds_path_override=Path("/tmp") / f"st-int-{tmp_path.name}.sock",
    )
    settings.ensure_directories()

    archive = Archive.open(settings, KEY, account_hash=ACCOUNT)
    archive.close()
    factory = lambda: Archive.open(settings, KEY, account_hash=ACCOUNT, initialize=False)

    bus = EventBus()
    source = ScriptedSource(
        [
            _snapshot(
                messages=[_message("100-1", timestamp="2026-01-01T00:00:00+00:00")],
                rooms=[Room("100", "Integration Room", "direct", "2026-01-01T00:00:00+00:00")],
            ),
            _snapshot(
                messages=[
                    _message("100-1", timestamp="2026-01-01T00:00:00+00:00"),
                    _message("100-2", timestamp="2026-01-01T00:05:00+00:00", text="new"),
                ],
                rooms=[Room("100", "Integration Room", "direct", "2026-01-01T00:05:00+00:00")],
            ),
        ]
    )
    poller = Poller(settings, factory, bus, source, clock=lambda: NOW, monotonic=lambda: 0.0)

    context = ApiContext(
        settings=settings,
        token=TOKEN,
        archive_factory=factory,
        bus=bus,
        poller=poller,
        health_provider=lambda: {"running": True, "kakaotalk_running": True,
                                 "permissions_ok": True, "polling": True,
                                 "last_sync_at": poller.stats.last_success_at},
        status_provider=lambda: {},
        webhook_count=lambda: {"total": 0, "active": 0},
        send_queue=None,
    )
    api = ApiService(context, build_router(context), host="127.0.0.1", port=0,
                     uds_path=settings.uds_path)
    api.start()
    try:
        yield api, poller, settings, factory
    finally:
        api.stop()
        poller.close()


def _http(service: ApiService, method: str, path: str, *, timeout: float = 2.0):
    headers = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close",
               f"Authorization: Bearer {TOKEN}"]
    with socket.create_connection((service.host, service.port), timeout=timeout) as sock:
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
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
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


def _get_json(service, path):
    status, body = _http(service, "GET", path)
    return status, json.loads(body) if body else None


def test_backfill_then_live_event_over_the_api(service) -> None:
    api, poller, _, _ = service

    # First poll: backfill, no events.
    first = poller.poll_once()
    assert first.backfill is True

    status, payload = _get_json(api, "/v1/status")
    assert status == 200
    assert payload["archive"]["rooms"] == 1
    assert payload["archive"]["messages"] == 1
    assert payload["archive"]["event_seq"] == 0

    # Second poll sees a new message and records exactly one event.
    second = poller.poll_once()
    assert second.created == 1

    status, payload = _get_json(api, "/v1/status")
    assert payload["archive"]["messages"] == 2
    assert payload["archive"]["event_seq"] == 1

    # The message is served with the documented DTO fields.
    status, page = _get_json(api, "/v1/rooms/100/messages?limit=10")
    assert [m["message_id"] for m in page["messages"]] == ["100-1", "100-2"]
    assert set(page["messages"][0]) >= {"message_id", "timestamp", "text", "is_placeholder"}

    # SSE replays the recorded event from the beginning.
    status, body = _http(api, "GET", "/v1/events?since=0")
    text = body.decode("utf-8", errors="replace")
    assert status == 200
    assert "event: message.created" in text
    assert '"message_id":"100-2"' in text


def test_room_metadata_survives_into_the_api(service) -> None:
    api, poller, _, _ = service
    poller.poll_once()

    status, payload = _get_json(api, "/v1/rooms")
    assert status == 200
    room = payload["rooms"][0]
    assert room["chat_id"] == "100"
    assert room["chat_type"] == "direct"
    assert room["message_count"] == 1


def test_health_reflects_poller_state(service) -> None:
    api, poller, _, _ = service
    poller.poll_once()

    status, body = _http(api, "GET", "/health")
    payload = json.loads(body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["last_sync_at"] is not None


def test_archive_stays_encrypted_on_disk(service) -> None:
    api, poller, settings, _ = service
    poller.poll_once()

    with open(settings.archive_path, "rb") as handle:
        assert not handle.read(16).startswith(b"SQLite format 3")
