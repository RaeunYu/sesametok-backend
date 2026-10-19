from __future__ import annotations

import time
from pathlib import Path

import pytest

from sesametok_backend.config import Settings
from sesametok_backend.core.archive import Archive
from sesametok_backend.core.events import EventBus
from sesametok_backend.core.poller import Poller
from sesametok_backend.kakao.reader import Snapshot
from sesametok_backend.models import Message, Room

KEY = bytes.fromhex("ab" * 32)
ACCOUNT = "account-hash"
NOW = 1_767_225_600  # 2026-01-01T00:00:00+00:00


class FakeSource:
    def __init__(self, snapshots) -> None:
        self._snapshots = list(snapshots)
        self.since_calls: list[int | None] = []

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        self.since_calls.append(since_epoch)
        return self._snapshots.pop(0)


class FlakySource:
    def __init__(self) -> None:
        self.calls = 0

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("kakao database is locked")
        return _snapshot()


class QuietSource:
    """소진되지 않는 소스: 폴러가 몇 번을 돌아도 같은 빈 스냅숏을 돌려준다."""

    def __init__(self) -> None:
        self.calls = 0

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        self.calls += 1
        return _snapshot()


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        poll_seconds=0.01,
        lookback_seconds=900,
        backfill_days=30,
        backfill_max=100_000,
    )
    base.update(overrides)
    return Settings(**base)


def _snapshot(messages=(), rooms=(), skipped=0) -> Snapshot:
    return Snapshot(
        rooms=tuple(rooms),
        messages=tuple(messages),
        skipped_rows=skipped,
        open_chat_ids=frozenset(),
    )


def _message(message_id: str, *, timestamp: str, text: str = "hello", **overrides) -> Message:
    base = dict(
        message_id=message_id,
        chat_id="100",
        chat_name="room",
        chat_type="direct",
        sender_id="500",
        sender_nickname="nick",
        timestamp=timestamp,
        text=text,
        message_type="text",
    )
    base.update(overrides)
    return Message(**base)


@pytest.fixture
def archive(tmp_path: Path):
    instance = Archive.open(_settings(tmp_path), KEY, account_hash=ACCOUNT)
    yield instance
    instance.close()


def _poller(settings, archive, source, bus=None, *, interval=None, monotonic=None) -> Poller:
    """폴러는 자신의 연결을 연다. 픽스처 연결은 검증용이다."""

    key = KEY
    return Poller(
        settings,
        lambda: Archive.open(settings, key, account_hash=ACCOUNT),
        bus or EventBus(),
        source,
        clock=lambda: NOW,
        monotonic=monotonic if monotonic is not None else (lambda: 0.0),
        interval=interval,
    )


def test_first_poll_backfills_without_events(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path)
    source = FakeSource([
        _snapshot(
            messages=[_message("100-1", timestamp="2026-01-01T00:00:00+00:00")],
            rooms=[Room("100", "room", "direct", "2026-01-01T00:00:00+00:00")],
            skipped=3,
        )
    ])
    poller = _poller(settings, archive, source)

    result = poller.poll_once()

    assert result.backfill is True
    assert result.created == 1
    assert result.skipped_rows == 3
    assert archive.count_messages() == 1
    assert archive.latest_seq() == 0  # 백필은 이벤트를 내보내지 않는다
    assert archive.poll_cursor() == NOW
    assert source.since_calls == [NOW - 30 * 86_400]


def test_second_poll_uses_lookback_and_emits_created(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path)
    first = _message("100-1", timestamp="2026-01-01T00:00:00+00:00")
    second = _message("100-2", timestamp="2026-01-01T00:05:00+00:00", text="new")
    source = FakeSource([_snapshot(messages=[first]), _snapshot(messages=[first, second])])
    bus = EventBus()
    subscription = bus.subscribe()
    poller = _poller(settings, archive, source, bus)

    poller.poll_once()
    result = poller.poll_once()

    assert result.backfill is False
    assert result.created == 1
    assert source.since_calls[1] == NOW - 900
    events = archive.events_since(0)
    assert [event.kind for event in events] == ["message.created"]
    assert events[0].payload["message_id"] == "100-2"
    assert subscription.get(timeout=0.1).seq == 1


def test_edited_message_emits_updated(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path)
    original = _message("100-1", timestamp="2026-01-01T00:00:00+00:00")
    edited = _message("100-1", timestamp="2026-01-01T00:00:00+00:00", text="edited")
    source = FakeSource([_snapshot(messages=[original]), _snapshot(messages=[edited])])
    poller = _poller(settings, archive, source)

    poller.poll_once()
    result = poller.poll_once()

    assert result.updated == 1
    assert [event.kind for event in archive.events_since(0)] == ["message.updated"]
    assert archive.message("100-1").text == "edited"


def test_room_rename_emits_room_updated(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path)
    source = FakeSource([
        _snapshot(rooms=[Room("100", "before", "direct")]),
        _snapshot(rooms=[Room("100", "after", "direct")]),
    ])
    poller = _poller(settings, archive, source)

    poller.poll_once()
    result = poller.poll_once()

    assert result.rooms_changed == 1
    events = archive.events_since(0)
    assert [event.kind for event in events] == ["room.updated"]
    assert archive.room("100").chat_name == "after"


def test_events_are_ordered_by_timestamp(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path)
    late = _message("100-9", timestamp="2026-01-01T00:09:00+00:00")
    early = _message("100-1", timestamp="2026-01-01T00:01:00+00:00")
    source = FakeSource([_snapshot(), _snapshot(messages=[late, early])])
    poller = _poller(settings, archive, source)

    poller.poll_once()
    poller.poll_once()

    assert [event.payload["message_id"] for event in archive.events_since(0)] == [
        "100-1",
        "100-9",
    ]


def test_backfill_cap_keeps_the_newest(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path, backfill_max=2)
    messages = [
        _message(f"100-{index}", timestamp=f"2026-01-01T00:0{index}:00+00:00")
        for index in range(1, 5)
    ]
    source = FakeSource([_snapshot(messages=messages)])
    poller = _poller(settings, archive, source)

    poller.poll_once()

    assert archive.count_messages() == 2
    assert archive.message("100-3") is not None
    assert archive.message("100-4") is not None
    assert archive.message("100-1") is None


def test_backfill_days_zero_means_everything(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path, backfill_days=0)
    source = FakeSource([_snapshot()])
    poller = _poller(settings, archive, source)

    poller.poll_once()

    assert source.since_calls == [None]


def test_pause_and_resume_toggle(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path)
    poller = _poller(settings, archive, FakeSource([_snapshot()]))

    assert poller.paused is False
    poller.pause()
    assert poller.paused is True
    assert poller.health()["polling"] is False
    poller.resume()
    assert poller.paused is False


def test_run_recovers_from_a_failure(tmp_path: Path, archive: Archive) -> None:
    settings = _settings(tmp_path, poll_seconds=0.01)
    source = FlakySource()
    poller = _poller(settings, archive, source)

    poller.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and poller.stats.polls < 1:
            time.sleep(0.02)
    finally:
        poller.stop()
        poller.join(timeout=3)

    assert poller.stats.polls >= 1
    assert poller.stats.consecutive_failures == 0
    assert source.calls >= 2  # 첫 호출은 실패했고 재시도는 성공했다
    assert poller.stopping is True


# --------------------------------------------------------------- 폴링 주기


def test_interval_defaults_to_settings_and_accepts_an_override(
    tmp_path: Path, archive: Archive
) -> None:
    settings = _settings(tmp_path, poll_seconds=10)
    from_settings = _poller(settings, archive, FakeSource([]))
    explicit = _poller(settings, archive, FakeSource([]), interval=30)

    assert from_settings.interval == 10.0
    assert explicit.interval == 30.0
    assert from_settings.health()["poll_seconds"] == 10.0
    assert explicit.health()["poll_seconds"] == 30.0


def test_set_interval_replaces_the_interval_and_keeps_other_behaviour(
    tmp_path: Path, archive: Archive
) -> None:
    poller = _poller(_settings(tmp_path, poll_seconds=10), archive, FakeSource([]))

    assert poller.set_interval(2) == 2.0
    assert poller.interval == 2.0
    assert poller.health()["poll_seconds"] == 2.0
    # stats/paused/수명주기 동작은 그대로다.
    assert poller.stats.polls == 0
    assert poller.paused is False
    assert poller.stopping is False
    assert poller.health()["polling"] is True


def test_set_interval_rejects_invalid_values(tmp_path: Path, archive: Archive) -> None:
    poller = _poller(_settings(tmp_path, poll_seconds=10), archive, FakeSource([]))

    for bad in (0, -1, float("nan"), float("inf"), "10", True, None):
        with pytest.raises(ValueError):
            poller.set_interval(bad)
    # 거부된 값은 주기를 바꾸지 않는다.
    assert poller.interval == 10.0


def test_interval_change_takes_effect_while_waiting(
    tmp_path: Path, archive: Archive
) -> None:
    """대기 중에 주기를 줄이면 60초를 기다리지 않고 곧바로 폴링한다."""

    settings = _settings(tmp_path, poll_seconds=60)
    source = QuietSource()
    poller = _poller(settings, archive, source, monotonic=time.monotonic)
    poller.start()
    try:
        assert _wait_until(lambda: poller.stats.polls >= 1)
        started = time.time()
        poller.set_interval(0.05)
        assert _wait_until(lambda: poller.stats.polls >= 3, timeout=3.0)
        assert time.time() - started < 3.0
        assert poller.interval == 0.05
        assert source.calls >= poller.stats.polls
    finally:
        poller.stop()
        poller.join(timeout=3)

    assert poller.stopping is True


def test_lengthening_the_interval_waits_instead_of_polling_immediately(
    tmp_path: Path, archive: Archive
) -> None:
    settings = _settings(tmp_path, poll_seconds=0.05)
    poller = _poller(settings, archive, QuietSource(), monotonic=time.monotonic)
    poller.start()
    try:
        assert _wait_until(lambda: poller.stats.polls >= 2)
        poller.set_interval(30)
        settled = poller.stats.polls
        time.sleep(0.2)
        # 늘어난 주기는 즉시 폴링을 강제하지 않는다(대기만 길어진다).
        assert poller.stats.polls in (settled, settled + 1)
        assert poller.interval == 30.0
    finally:
        poller.stop()
        poller.join(timeout=3)
