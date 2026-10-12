"""SSE 및 웹훅 소비자를 위한 프로세스 내 이벤트 팬아웃.

아카이브가 영속적인 진실의 원천(``events`` 테이블)이며, 이 버스는 실시간 알림만
전달한다. 뒤처진 구독자는 큐에서 가장 오래된 이벤트가 버려지고 시퀀스 번호로
아카이브에서 이어받을 수 있으므로, 느린 클라이언트가 폴러를 막는 일은 없다.
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
        """활성 구독자에게 팬아웃한다. 전달받은 구독자 수를 반환한다."""

        with self._lock:
            subscriptions = list(self._subscriptions.values())

        delivered = 0
        for subscription in subscriptions:
            if subscription.closed:
                continue
            try:
                subscription.queue.put_nowait(event)
            except queue.Full:
                # 최신 이벤트가 도착하도록 가장 오래된 것을 버린다.
                try:
                    subscription.queue.get_nowait()
                    subscription.queue.put_nowait(event)
                except (queue.Empty, queue.Full):  # pragma: no cover - 경합 가능성
                    pass
                subscription.dropped += 1
            delivered += 1
        return delivered
