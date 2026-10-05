"""In-process event fan-out for SSE and webhook consumers.

The archive is the durable source of truth (``events`` table); this bus only
delivers live notifications. A subscriber that falls behind has its oldest
queued event dropped and can still resume from the archive by sequence number,
so a slow client can never stall the poller.
"""

from __future__ import annotations

import itertools
import queue
import threading
from dataclasses import dataclass, field

from ..models import Event

DEFAULT_QUEUE_SIZE = 2000


@dataclass
class Subscription:
    id: int
    queue: "queue.Queue[Event]" = field(default_factory=queue.Queue)
    dropped: int = 0
    closed: bool = False

    def get(self, timeout: float | None = None) -> Event | None:
        if self.closed:
            return None
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, limit: int = 500) -> list[Event]:
        events: list[Event] = []
        while len(events) < limit:
            try:
                events.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return events

    def close(self) -> None:
        self.closed = True


class EventBus:
    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[int, Subscription] = {}
        self._ids = itertools.count(1)
        self._queue_size = queue_size

    def subscribe(self) -> Subscription:
        with self._lock:
            subscription = Subscription(
                id=next(self._ids), queue=queue.Queue(maxsize=self._queue_size)
            )
            self._subscriptions[subscription.id] = subscription
            return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscriptions.pop(subscription.id, None)
        subscription.close()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def publish(self, event: Event) -> int:
        """Fan out to live subscribers. Returns how many received it."""

        with self._lock:
            subscriptions = list(self._subscriptions.values())

        delivered = 0
        for subscription in subscriptions:
            if subscription.closed:
                continue
            try:
                subscription.queue.put_nowait(event)
            except queue.Full:
                # Drop the oldest so the newest events still arrive.
                try:
                    subscription.queue.get_nowait()
                    subscription.queue.put_nowait(event)
                except (queue.Empty, queue.Full):  # pragma: no cover - racy
                    pass
                subscription.dropped += 1
            delivered += 1
        return delivered
