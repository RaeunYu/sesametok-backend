from __future__ import annotations

from sesametok_backend.core.events import EventBus
from sesametok_backend.models import Event


def test_publish_reaches_subscribers() -> None:
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe()
    event = Event(seq=1, kind="message.created", payload={"a": 1})

    assert bus.publish(event) == 2
    assert first.get(timeout=0.1) == event
    assert second.get(timeout=0.1) == event


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    bus.unsubscribe(subscription)

    assert bus.subscriber_count == 0
    assert bus.publish(Event(seq=1, kind="message.created")) == 0
    assert subscription.get(timeout=0.01) is None


def test_drain_returns_queued_events_in_order() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    bus.publish(Event(seq=1, kind="message.created"))
    bus.publish(Event(seq=2, kind="message.updated"))

    assert [event.seq for event in subscription.drain()] == [1, 2]


def test_full_queue_drops_the_oldest_and_counts_it() -> None:
    bus = EventBus(queue_size=2)
    subscription = bus.subscribe()
    for seq in range(1, 5):
        bus.publish(Event(seq=seq, kind="message.created"))

    remaining = [event.seq for event in subscription.drain()]
    assert remaining == [3, 4]
    assert subscription.dropped == 2


def test_closed_subscription_is_skipped() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    subscription.close()

    assert bus.publish(Event(seq=1, kind="message.created")) == 0
