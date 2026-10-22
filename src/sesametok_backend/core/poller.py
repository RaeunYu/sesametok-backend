"""폴링 오케스트레이션(스레드 B).

각 폴링은 ``cursor − lookback`` 부터 다시 읽어 늦게 도착한 메시지, 수정, 방 이름
변경을 잡아낸다. 최초 폴링은 범위가 제한된 백필이며 의도적으로 이벤트를
**전혀** 내보내지 않는다. 백필된 기록은 "새 것"이 아니기 때문이다. 실패가
스레드를 죽이는 일은 없으며, 성능 저하 상태를 기록하고 백오프한다.

폴러는 메시지, 방, 이벤트의 유일한 작성자다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from ..config import Settings
from ..models import Message
from .archive import Archive
from .events import EventBus
from .preferences import DEFAULT_POLL_SECONDS, coerce_poll_seconds
from .snapshot import MessageDiff, RoomDiff

logger = logging.getLogger("sesametok_backend.core.poller")

MAX_BACKOFF_SECONDS = 60.0
PRUNE_INTERVAL_SECONDS = 86_400


class PollSource(Protocol):
    def read_snapshot(self, *, since_epoch: int | None = None): ...


def rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@dataclass
class PollStats:
    polls: int = 0
    created: int = 0
    updated: int = 0
    rooms_changed: int = 0
    skipped_rows: int = 0
    last_poll_at: str | None = None
    last_success_at: str | None = None
    last_duration_ms: int | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    backfilled: bool = False

    def as_dict(self) -> dict:
        return {
            "polls": self.polls,
            "created": self.created,
            "updated": self.updated,
            "rooms_changed": self.rooms_changed,
            "skipped_rows": self.skipped_rows,
            "last_poll_at": self.last_poll_at,
            "last_success_at": self.last_success_at,
            "last_duration_ms": self.last_duration_ms,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "backfilled": self.backfilled,
        }


@dataclass(frozen=True)
class PollResult:
    backfill: bool
    created: int
    updated: int
    rooms_changed: int
    skipped_rows: int
    duration_ms: int


class Poller(threading.Thread):
    def __init__(
        self,
        settings: Settings,
        archive_factory: Callable[[], Archive],
        bus: EventBus,
        source: PollSource,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        interval: float | None = None,
    ) -> None:
        super().__init__(name="sesametok-poller", daemon=True)
        self.settings = settings
        self.bus = bus
        self.source = source
        self.stats = PollStats()
        # SQLite 연결은 스레드에 귀속되므로, 폴러는 자신의 스레드 안에서 처음
        # 사용할 때 자신의 연결을 연다.
        self._archive_factory = archive_factory
        self._archive_instance: Archive | None = None
        self._clock = clock
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self._last_prune: float | None = None
        # 폴링 주기는 실행 중에 바뀔 수 있다. ``_interval_version``은 대기 중인
        # 루프가 "주기가 바뀌었는지"를 알아채게 하는 표식이다(잠금 없이 읽어도
        # 되는 단조 증가 정수).
        self._interval_lock = threading.Lock()
        self._interval = self._initial_interval(interval)
        self._interval_version = 0

    def _initial_interval(self, interval: float | None) -> float:
        if interval is not None:
            resolved = coerce_poll_seconds(interval)
            if resolved is None:
                raise ValueError("폴링 주기는 0보다 큰 유한한 초 단위 숫자여야 합니다")
            return resolved
        return coerce_poll_seconds(self.settings.poll_seconds) or DEFAULT_POLL_SECONDS

    # ------------------------------------------------------------- 아카이브

    @property
    def archive(self) -> Archive:
        if self._archive_instance is None:
            self._archive_instance = self._archive_factory()
        return self._archive_instance

    def close(self) -> None:
        if self._archive_instance is not None:
            self._archive_instance.close()
            self._archive_instance = None

    # ------------------------------------------------------------- 제어

    def trigger(self) -> None:
        """루프에 즉시 폴링하도록 요청한다(``POST /v1/sync`` 에서 사용)."""

        self._wake.set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self.trigger()

    @property
    def interval(self) -> float:
        """현재 폴링 주기(초)."""

        with self._interval_lock:
            return self._interval

    def set_interval(self, seconds: float) -> float:
        """폴링 주기를 즉시 바꾼다(어느 스레드에서든 안전).

        대기 중이면 대기를 깨워 새 주기 기준으로 남은 시간을 다시 계산하므로,
        주기를 줄이면 곧바로 다음 폴링이 일어나고 늘리면 남은 시간이 늘어난다.
        ``trigger()``처럼 즉시 폴링을 강제하지는 않는다(주기만 바꾼다). 유효하지
        않은 값은 :class:`ValueError`다. 적용된 주기를 반환한다.
        """

        value = coerce_poll_seconds(seconds)
        if value is None:
            raise ValueError("폴링 주기는 0보다 큰 유한한 초 단위 숫자여야 합니다")
        with self._interval_lock:
            if value == self._interval:
                return value
            self._interval = value
            self._interval_version += 1
        logger.info("폴링 주기를 %.1f초로 바꿨습니다", value)
        # 대기 중인 루프를 깨워 새 주기로 남은 시간을 다시 계산하게 한다.
        self._wake.set()
        return value

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ 실행

    def run(self) -> None:
        logger.info(
            "폴러 시작: %.1f초마다, lookback %d초",
            self.interval,
            self.settings.lookback_seconds,
        )
        backoff = 1.0
        try:
            while not self._stop.is_set():
                if self._paused.is_set():
                    self._stop.wait(0.5)
                    continue
                try:
                    self.poll_once()
                except Exception as exc:  # 스레드를 살려 두고 성능 저하를 보고한다
                    self.stats.consecutive_failures += 1
                    self.stats.last_error = type(exc).__name__
                    logger.warning(
                        "폴링 실패 (%s); %.0f초 백오프합니다.", type(exc).__name__, backoff
                    )
                    self._stop.wait(min(backoff, MAX_BACKOFF_SECONDS))
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue

                backoff = 1.0
                self.stats.consecutive_failures = 0
                self.stats.last_error = None
                self._wake.clear()
                self._wait_for_next_poll()
        finally:
            self.close()
            logger.info("폴러 중지됨")

    def _wait_for_next_poll(self) -> None:
        """다음 폴링까지 기다린다. 대기 중에 주기가 바뀌면 남은 시간을 다시 계산한다.

        이미 기다린 시간은 새 주기에서 뺀다. 그래서 60초에서 2초로 줄이면 대기가
        곧바로 끝나고(남은 시간 0 이하), 2초에서 60초로 늘리면 남은 시간이 늘어난다.
        ``trigger()``는 즉시 폴링하도록 대기를 끝내고, ``stop()``은 루프를 끝낸다.
        """

        started = self._monotonic()
        version = self._interval_version
        while not self._stop.is_set():
            remaining = self.interval - (self._monotonic() - started)
            if remaining <= 0:
                return
            if not self._wake.wait(remaining):
                return  # 주기가 다 됐다
            self._wake.clear()
            if self._stop.is_set() or self._interval_version == version:
                return  # 종료 요청이거나 trigger()다
            # set_interval()이 대기를 깼다: 새 주기로 남은 시간을 다시 계산한다.
            version = self._interval_version

    # -------------------------------------------------------------- 폴링 1회

    def poll_once(self) -> PollResult:
        started = self._monotonic()
        now = int(self._clock())
        cursor = self.archive.poll_cursor()
        backfill = cursor is None

        since: int | None
        if backfill:
            since = (
                now - self.settings.backfill_days * 86_400
                if self.settings.backfill_days > 0
                else None
            )
        else:
            since = cursor - self.settings.lookback_seconds

        snapshot = self.source.read_snapshot(since_epoch=since)
        messages = list(snapshot.messages)
        if backfill and self.settings.backfill_max > 0:
            if len(messages) > self.settings.backfill_max:
                logger.info(
                    "백필 상한 적용: 메시지 %d개 중 최신 %d개만 유지합니다.",
                    len(messages),
                    self.settings.backfill_max,
                )
                messages = messages[-self.settings.backfill_max :]

        diff = self.archive.apply_message_diff(messages)
        room_diff = self.archive.upsert_rooms(snapshot.rooms)
        self.archive.set_poll_state(cursor=now, last_success_at=rfc3339(now))
        self.archive.commit()

        if not backfill:
            self._publish(diff, room_diff)

        self._maybe_prune(now)

        duration_ms = int((self._monotonic() - started) * 1000)
        self.stats.polls += 1
        self.stats.created += len(diff.created)
        self.stats.updated += len(diff.updated)
        self.stats.rooms_changed += len(room_diff.changed)
        self.stats.skipped_rows = snapshot.skipped_rows
        self.stats.last_poll_at = rfc3339(now)
        self.stats.last_success_at = self.stats.last_poll_at
        self.stats.last_duration_ms = duration_ms
        self.stats.backfilled = self.stats.backfilled or backfill

        if backfill:
            logger.info(
                "최초 백필로 메시지 %d개, 방 %d개를 저장했다(이벤트 없음)",
                len(diff.created),
                len(snapshot.rooms),
            )

        return PollResult(
            backfill=backfill,
            created=len(diff.created),
            updated=len(diff.updated),
            rooms_changed=len(room_diff.changed),
            skipped_rows=snapshot.skipped_rows,
            duration_ms=duration_ms,
        )

    def _publish(self, diff: MessageDiff, room_diff: RoomDiff) -> None:
        changes: list[tuple[str, Message]] = [
            ("message.created", message) for message in diff.created
        ] + [("message.updated", message) for message in diff.updated]
        changes.sort(key=lambda item: (item[1].timestamp, item[1].chat_id, item[1].message_id))

        published = []
        for kind, message in changes:
            published.append(self.archive.append_event(kind, message.to_dict()))
        for room in room_diff.changed:
            published.append(self.archive.append_event("room.updated", room.to_dict()))
        if published:
            self.archive.commit()
        for event in published:
            self.bus.publish(event)

    def _maybe_prune(self, now: int) -> None:
        if self._last_prune is not None and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        removed = self.archive.prune_events(
            days=self.settings.event_retention_days,
            max_count=self.settings.event_retention_max,
            now=rfc3339(now),
        )
        if removed:
            logger.info("만료된 이벤트 %d개를 정리했다", removed)

    # --------------------------------------------------------------- 상태

    def health(self) -> dict:
        return {
            "polling": not self.paused and not self.stopping,
            "paused": self.paused,
            "poll_seconds": self.interval,
            "last_success_at": self.stats.last_success_at,
            "consecutive_failures": self.stats.consecutive_failures,
            "last_error": self.stats.last_error,
        }
