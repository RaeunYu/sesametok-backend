from __future__ import annotations

import os
from pathlib import Path

import pytest

from sesametok_backend.config import Settings
from sesametok_backend.core.archive import Archive
from sesametok_backend.models import Message, Room

KEY = bytes.fromhex("ab" * 32)
OTHER_KEY = bytes.fromhex("cd" * 32)
ACCOUNT = "account-hash"


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")
    base.update(overrides)
    return Settings(**base)


def _message(message_id: str, *, chat_id: str = "100", timestamp: str | None = None, **overrides):
    base = dict(
        message_id=message_id,
        chat_id=chat_id,
        chat_name="room",
        chat_type="direct",
        sender_id="500",
        sender_nickname="nick",
        timestamp=timestamp or f"2026-01-01T09:00:{message_id.split('-')[-1].zfill(2)}+00:00",
        text="hello",
        message_type="text",
    )
    base.update(overrides)
    return Message(**base)


@pytest.fixture
def archive(tmp_path: Path):
    instance = Archive.open(_settings(tmp_path), KEY, account_hash=ACCOUNT)
    yield instance
    instance.close()


def test_schema_is_created_and_file_is_private(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    instance = Archive.open(settings, KEY, account_hash=ACCOUNT)
    try:
        assert instance.count_messages() == 0
        assert instance.count_rooms() == 0
        assert os.stat(settings.archive_path).st_mode & 0o777 == 0o600
    finally:
        instance.close()


def test_wrong_archive_key_is_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    Archive.open(settings, KEY, account_hash=ACCOUNT).close()

    with pytest.raises(Exception):
        Archive.open(settings, OTHER_KEY, account_hash=ACCOUNT)


def test_insert_then_update_then_unchanged(archive: Archive) -> None:
    first = archive.apply_message_diff([_message("100-1")])
    assert len(first.created) == 1 and first.updated == ()

    again = archive.apply_message_diff([_message("100-1")])
    assert again.created == () and again.updated == () and again.unchanged == 1

    edited = archive.apply_message_diff([_message("100-1", text="edited")])
    assert len(edited.updated) == 1
    assert archive.count_messages() == 1
    assert archive.message("100-1").text == "edited"


def test_messages_are_partitioned_by_account(archive: Archive) -> None:
    archive.apply_message_diff([_message("100-1")])
    assert archive.count_messages() == 1
    assert archive.count_messages("100") == 1
    assert archive.count_messages("999") == 0


def test_room_upsert_and_last_message_never_regresses(archive: Archive) -> None:
    archive.upsert_rooms([Room("100", "room", "direct", "2026-01-02T00:00:00+00:00")])
    changed = archive.upsert_rooms([Room("100", "renamed", "group", "2026-01-01T00:00:00+00:00")])

    assert [room.chat_id for room in changed.changed] == ["100"]
    room = archive.room("100")
    assert room.chat_name == "renamed"
    assert room.chat_type == "group"
    assert room.last_message_at == "2026-01-02T00:00:00+00:00"


def test_room_message_count_reflects_messages(archive: Archive) -> None:
    archive.upsert_rooms([Room("100", "room", "direct")])
    archive.apply_message_diff([_message("100-1"), _message("100-2")])
    archive.commit()

    assert archive.room("100").message_count == 2


def test_pagination_walks_backwards(archive: Archive) -> None:
    for index in range(1, 6):
        archive.apply_message_diff([_message(f"100-{index}")])
    archive.commit()

    page = archive.messages("100", limit=2)
    assert [m.message_id for m in page.messages] == ["100-4", "100-5"]
    assert page.has_more is True
    assert page.next_before == "100-4"

    older = archive.messages("100", limit=2, before=page.next_before)
    assert [m.message_id for m in older.messages] == ["100-2", "100-3"]
    assert older.has_more is True
    assert older.next_before == "100-2"

    oldest = archive.messages("100", limit=2, before=older.next_before)
    assert [m.message_id for m in oldest.messages] == ["100-1"]
    assert oldest.has_more is False
    assert oldest.next_before is None


def test_pagination_unknown_cursor_is_empty(archive: Archive) -> None:
    archive.apply_message_diff([_message("100-1")])
    archive.commit()

    page = archive.messages("100", before="100-999")
    assert page.messages == ()
    assert page.has_more is False


def test_since_and_until_filters(archive: Archive) -> None:
    archive.apply_message_diff(
        [
            _message("100-1", timestamp="2026-01-01T00:00:00+00:00"),
            _message("100-2", timestamp="2026-01-02T00:00:00+00:00"),
            _message("100-3", timestamp="2026-01-03T00:00:00+00:00"),
        ]
    )
    archive.commit()

    window = archive.messages(
        "100", since="2026-01-02T00:00:00+00:00", until="2026-01-03T00:00:00+00:00"
    )
    assert [m.message_id for m in window.messages] == ["100-2"]


def test_message_lookup_by_composite_id(archive: Archive) -> None:
    archive.apply_message_diff([_message("100-42")])
    archive.commit()

    assert archive.message("100-42").chat_id == "100"
    assert archive.message("100-99") is None
    assert archive.message("nonsense") is None


def test_events_append_and_replay(archive: Archive) -> None:
    first = archive.append_event("message.created", {"chat_id": "100"})
    second = archive.append_event("send.result", {"result": "sent"})
    archive.commit()

    assert (first.seq, second.seq) == (1, 2)
    assert archive.latest_seq() == 2
    assert archive.oldest_seq() == 1
    assert [event.kind for event in archive.events_since(0)] == [
        "message.created",
        "send.result",
    ]
    assert [event.seq for event in archive.events_since(1)] == [2]


def test_event_pruning_by_age_and_count(archive: Archive) -> None:
    for index in range(3):
        archive.append_event("message.created", {"n": index}, created_at="2020-01-01T00:00:00+00:00")
    for index in range(3):
        archive.append_event("message.created", {"n": index}, created_at="2026-01-01T00:00:00+00:00")
    archive.commit()

    removed = archive.prune_events(days=1, max_count=2, now="2026-01-01T12:00:00+00:00")

    assert removed == 4  # 만료 3개 + 최신 2개를 넘어선 1개
    assert [event.payload["n"] for event in archive.events_since(0)] == [1, 2]


def test_poll_state_round_trip(archive: Archive) -> None:
    assert archive.poll_cursor() is None
    assert archive.last_success_at() is None

    archive.set_poll_state(cursor=1234, last_success_at="2026-01-01T00:00:00+00:00")
    archive.commit()

    assert archive.poll_cursor() == 1234
    assert archive.last_success_at() == "2026-01-01T00:00:00+00:00"


def test_webhook_lifecycle(archive: Archive) -> None:
    created = archive.add_webhook(
        webhook_id="hook-1", url="http://127.0.0.1:9999/x", secret="s3cret",
        kinds=["message.created"],
    )
    archive.commit()

    assert created["secret"] == "s3cret"
    assert created["active"] is True
    assert created["cursor_seq"] == 0

    listed = archive.list_webhooks()
    assert len(listed) == 1
    assert "secret" not in listed[0]

    assert archive.set_webhook_active("hook-1", False) is True
    assert archive.get_webhook("hook-1")["active"] is False

    archive.set_webhook_cursor("hook-1", 7)
    archive.record_webhook_result("hook-1", success=False, error="connection refused")
    archive.commit()
    record = archive.get_webhook("hook-1")
    assert record["cursor_seq"] == 7
    assert record["last_error"] == "connection refused"

    archive.record_webhook_result("hook-1", success=True)
    archive.commit()
    assert archive.get_webhook("hook-1")["last_success_at"] is not None
    assert archive.get_webhook("hook-1")["last_error"] is None

    assert archive.delete_webhook("hook-1") is True
    assert archive.get_webhook("hook-1") is None
    assert archive.delete_webhook("hook-1") is False


def test_send_request_idempotency_record(archive: Archive) -> None:
    assert archive.get_send_request("req-1") is None

    archive.record_send_request(
        client_request_id="req-1", chat_id="100", mode="send", result="pending"
    )
    archive.commit()
    assert archive.get_send_request("req-1")["result"] == "pending"

    archive.complete_send_request("req-1", "sent")
    archive.commit()
    record = archive.get_send_request("req-1")
    assert record["result"] == "sent"
    assert record["completed_at"] is not None
