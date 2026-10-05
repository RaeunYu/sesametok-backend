from __future__ import annotations

from pathlib import Path

import pytest

from sesametok_backend.config import Settings
from sesametok_backend.core.archive import Archive
from sesametok_backend.core.events import EventBus
from sesametok_backend.core.send_queue import (
    SendOutcome,
    SendQueue,
    SendRequest,
    SendResult,
)
from sesametok_backend.models import Room
from sesametok_backend.send.errors import SendError

KEY = bytes.fromhex("ab" * 32)
ACCOUNT = "account-hash"


class FakeSender:
    def __init__(self, outcome: SendOutcome | None = None) -> None:
        self.calls: list[tuple] = []
        self.outcome = outcome or SendOutcome("sent")

    def send_text(self, target, text, *, mode, background_only):
        self.calls.append(("text", target.chat_id, text, mode, background_only))
        return self.outcome

    def send_image(self, target, path, *, background_only):
        self.calls.append(("image", target.chat_id, str(path), background_only))
        return self.outcome


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        max_sends_per_minute=3,
        max_sends_per_room_per_minute=2,
    )
    base.update(overrides)
    return Settings(**base)


def _factory(settings: Settings):
    return lambda: Archive.open(settings, KEY, account_hash=ACCOUNT, initialize=False)


def _setup(tmp_path: Path, **overrides):
    settings = _settings(tmp_path, **overrides)
    archive = Archive.open(settings, KEY, account_hash=ACCOUNT)
    archive.upsert_rooms(
        [
            Room("100", "Direct Room", "direct", "2026-01-02T00:00:00+00:00"),
            Room("200", "Group Room", "group", "2026-01-01T00:00:00+00:00"),
        ]
    )
    archive.commit()
    archive.close()
    sender = FakeSender()
    queue = SendQueue(settings, _factory(settings), EventBus(), sender)
    return settings, queue, sender


def test_dry_run_resolves_without_sending(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)

    result = queue.process(SendRequest(mode="dry_run", chat_id="100", text="hi"))

    assert result.result == "dry_run"
    assert result.room == "Direct Room"
    assert result.chars == 2
    assert sender.calls == []


def test_send_requires_consent(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)

    with pytest.raises(SendError) as excinfo:
        queue.process(SendRequest(mode="send", chat_id="100", text="hi"))

    assert excinfo.value.code == "no_consent"
    assert sender.calls == []


def test_dry_run_does_not_require_consent(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    queue.process(SendRequest(mode="dry_run", chat_id="100", text="hi"))
    assert sender.calls == []


def test_send_delivers_and_records(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    archive = _factory(settings)()
    try:
        subscription = EventBus()
    finally:
        archive.close()

    result = queue.process(
        SendRequest(
            mode="send",
            chat_id="100",
            text="hello",
            accept_use_policy=True,
            client_request_id="req-1",
        )
    )

    assert result.result == "sent"
    assert result.room == "Direct Room"
    assert sender.calls == [("text", "100", "hello", "send", False)]

    archive = _factory(settings)()
    try:
        record = archive.get_send_request("req-1")
        assert record["result"] == "sent"
        events = archive.events_since(0)
        assert [event.kind for event in events] == ["send.result"]
        assert events[0].payload["chars"] == 5
        assert "hello" not in str(events[0].payload)
    finally:
        archive.close()


def test_draft_mode_is_passed_through(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    queue.process(
        SendRequest(
            mode="draft", chat_id="100", text="draft text", accept_use_policy=True
        )
    )

    assert sender.calls[0][3] == "draft"


def test_repeating_a_client_request_id_does_not_resend(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    request = SendRequest(
        mode="send",
        chat_id="100",
        text="hello",
        accept_use_policy=True,
        client_request_id="req-same",
    )

    first = queue.process(request)
    second = queue.process(request)

    assert first.result == "sent"
    assert second.result == "sent"
    assert second.code == "duplicate_request"
    assert len(sender.calls) == 1


def test_pending_previous_request_is_reported_as_unconfirmed(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    archive = _factory(settings)()
    try:
        archive.record_send_request(
            client_request_id="req-pending", chat_id="100", mode="send", result="pending"
        )
        archive.commit()
    finally:
        archive.close()

    with pytest.raises(SendError) as excinfo:
        queue.process(
            SendRequest(
                mode="send",
                chat_id="100",
                text="hi",
                accept_use_policy=True,
                client_request_id="req-pending",
            )
        )

    assert excinfo.value.code == "unconfirmed"
    assert sender.calls == []


def test_global_rate_limit(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path, max_sends_per_minute=2)

    def send(target_chat: str) -> None:
        queue.process(
            SendRequest(
                mode="send", chat_id=target_chat, text="x", accept_use_policy=True
            )
        )

    send("100")
    send("200")
    with pytest.raises(SendError) as excinfo:
        send("100")
    assert excinfo.value.code == "rate_limited"


def test_per_room_rate_limit(tmp_path: Path) -> None:
    settings, queue, sender = _setup(
        tmp_path, max_sends_per_minute=10, max_sends_per_room_per_minute=1
    )

    queue.process(
        SendRequest(mode="send", chat_id="100", text="x", accept_use_policy=True)
    )
    with pytest.raises(SendError) as excinfo:
        queue.process(
            SendRequest(mode="send", chat_id="100", text="x", accept_use_policy=True)
        )
    assert excinfo.value.code == "rate_limited"


def test_dry_run_does_not_consume_rate_limit(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path, max_sends_per_minute=1)
    for _ in range(3):
        queue.process(SendRequest(mode="dry_run", chat_id="100", text="x"))
    queue.process(
        SendRequest(mode="send", chat_id="100", text="x", accept_use_policy=True)
    )


def test_image_send_uses_the_image_path(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    image = tmp_path / "picture.png"
    image.write_bytes(b"\x89PNG")

    result = queue.process(
        SendRequest(mode="send", chat_id="100", image_path=image, accept_use_policy=True)
    )

    assert result.result == "sent"
    assert sender.calls[0][0] == "image"


def test_text_and_image_together_are_rejected(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    image = tmp_path / "picture.png"
    image.write_bytes(b"\x89PNG")

    with pytest.raises(SendError) as excinfo:
        queue.process(
            SendRequest(
                mode="send",
                chat_id="100",
                text="hi",
                image_path=image,
                accept_use_policy=True,
            )
        )
    assert excinfo.value.code == "bad_request"


def test_missing_payload_is_rejected(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    with pytest.raises(SendError):
        queue.process(SendRequest(mode="send", chat_id="100", accept_use_policy=True))


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    with pytest.raises(SendError) as excinfo:
        queue.process(SendRequest(mode="post", chat_id="100", text="x"))
    assert excinfo.value.code == "bad_mode"


def test_unknown_room_is_reported(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    with pytest.raises(SendError) as excinfo:
        queue.process(SendRequest(mode="dry_run", chat_id="999", text="x"))
    assert excinfo.value.code == "room_not_found"


def test_unconfirmed_result_is_surfaced(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    sender.outcome = SendOutcome("unconfirmed", "not_sent", "message stayed in the box")

    result = queue.process(
        SendRequest(mode="send", chat_id="100", text="x", accept_use_policy=True)
    )

    assert result.result == "unconfirmed"
    assert result.code == "not_sent"
    assert queue.stats.last_result == "unconfirmed"


def test_submit_runs_the_worker_thread(tmp_path: Path) -> None:
    settings, queue, sender = _setup(tmp_path)
    try:
        result = queue.submit(
            SendRequest(
                mode="send", chat_id="100", text="threaded", accept_use_policy=True
            ),
            timeout=10,
        )
    finally:
        queue.stop()
        queue.join(timeout=5)

    assert result.result == "sent"
    assert sender.calls[0][2] == "threaded"


def test_result_dict_shape(tmp_path: Path) -> None:
    payload = SendResult(result="sent", room="Room", chars=3).to_dict()
    assert payload == {"result": "sent", "room": "Room", "chars": 3, "client_request_id": None}
