"""Polling orchestration (thread B).

Each poll re-reads ``cursor − lookback`` so late arrivals, edits, and room renames
are caught. The very first poll is a bounded backfill and deliberately emits
**no** events: backfilled history is not "new". Failures never kill the thread;
they raise a degraded state and back off.

The poller is the only writer for messages, rooms, and events.
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
    ) -> None:
        super().__init__(name="sesametok-poller", daemon=True)
        self.settings = settings
        self.bus = bus
        self.source = source
        self.stats = PollStats()
        # SQLite connections are thread-affine, so the poller opens its own
        # connection on first use inside its own thread.
        self._archive_factory = archive_factory
        self._archive_instance: Archive | None = None
        self._clock = clock
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self._last_prune: float | None = None

    # ------------------------------------------------------------- archive

    @property
    def archive(self) -> Archive:
        if self._archive_instance is None:
            self._archive_instance = self._archive_factory()
        return self._archive_instance

    def close(self) -> None:
        if self._archive_instance is not None:
            self._archive_instance.close()
            self._archive_instance = None

    # ------------------------------------------------------------- controls

    def trigger(self) -> None:
        """Ask the loop to poll immediately (used by ``POST /v1/sync``)."""

        self._wake.set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self.trigger()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        logger.info(
            "poller started: every %.1fs, lookback %ds",
            self.settings.poll_seconds,
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
                except Exception as exc:  # keep the thread alive, report degraded
                    self.stats.consecutive_failures += 1
                    self.stats.last_error = type(exc).__name__
                    logger.warning(
                        "poll failed (%s); backing off %.0fs", type(exc).__name__, backoff
                    )
                    self._stop.wait(min(backoff, MAX_BACKOFF_SECONDS))
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue

                backoff = 1.0
                self.stats.consecutive_failures = 0
                self.stats.last_error = None
                self._wake.clear()
                self._wake.wait(timeout=self.settings.poll_seconds)
        finally:
            self.close()
            logger.info("poller stopped")

    # -------------------------------------------------------------- one poll

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
                    "backfill capped: %d messages, keeping the newest %d",
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
                "initial backfill stored %d messages, %d rooms (no events)",
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
            logger.info("pruned %d expired event(s)", removed)

    # --------------------------------------------------------------- health

    def health(self) -> dict:
        return {
            "polling": not self.paused and not self.stopping,
            "paused": self.paused,
            "poll_seconds": self.settings.poll_seconds,
            "last_success_at": self.stats.last_success_at,
            "consecutive_failures": self.stats.consecutive_failures,
            "last_error": self.stats.last_error,
        }
