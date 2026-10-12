from __future__ import annotations

import hmac
import hashlib
import json
import time
from pathlib import Path

import pytest

from sesametok_backend.api.webhooks import (
    SEQUENCE_HEADER,
    SIGNATURE_HEADER,
    DeliveryResult,
    WebhookDispatcher,
    sign,
    validate_webhook_url,
)
from sesametok_backend.api.errors import ApiError
from sesametok_backend.config import Settings
from sesametok_backend.core.archive import Archive
from sesametok_backend.core.events import EventBus

KEY = bytes.fromhex("ab" * 32)
ACCOUNT = "account-hash"
SECRET = "s" * 32


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(data_dir=tmp_path / "data", log_dir=tmp_path / "logs", webhook_max_attempts=3)
    base.update(overrides)
    return Settings(**base)


def _archive(settings: Settings, *, events: int = 2, kinds=None, active: bool = True):
    archive = Archive.open(settings, KEY, account_hash=ACCOUNT)
    archive.add_webhook(
        webhook_id="hook-1",
        url="http://127.0.0.1:9999/hook",
        secret=SECRET,
        kinds=kinds if kinds is not None else ["message.created"],
    )
    for index in range(events):
        archive.append_event("message.created", {"n": index})
    archive.append_event("message.updated", {"n": 99})
    if not active:
        archive.set_webhook_active("hook-1", False)
    archive.commit()
    archive.close()
    return lambda: Archive.open(settings, KEY, account_hash=ACCOUNT, initialize=False)


class Recorder:
    def __init__(self, results=None) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []
        self.results = list(results or [])

    def __call__(self, url, body, headers, timeout) -> DeliveryResult:
        self.calls.append((url, body, headers))
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(True)


def test_successful_delivery_advances_cursor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    factory = _archive(settings)
    recorder = Recorder()
    dispatcher = WebhookDispatcher(settings, factory, EventBus(), sender=recorder)

    delivered = dispatcher.dispatch_once(now=time.time())

    assert delivered == 2  # message.created 이벤트 두 개
    archive = factory()
    try:
        hook = archive.get_webhook("hook-1")
        assert hook["cursor_seq"] == archive.latest_seq()
        assert hook["last_success_at"] is not None
        assert hook["last_error"] is None
    finally:
        archive.close()

    url, body, headers = recorder.calls[0]
    assert url == "http://127.0.0.1:9999/hook"
    assert headers[SIGNATURE_HEADER] == sign(SECRET, body)
    assert headers[SEQUENCE_HEADER] == "1"
    payload = json.loads(body)
    assert payload["kind"] == "message.created"


def test_signature_is_over_the_exact_body() -> None:
    body = b'{"seq":1}'
    expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert sign(SECRET, body) == expected


def test_unsubscribed_kinds_are_skipped_without_delivery(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    factory = _archive(settings, events=1)
    recorder = Recorder()
    dispatcher = WebhookDispatcher(settings, factory, EventBus(), sender=recorder)

    dispatcher.dispatch_once(now=time.time())

    assert [json.loads(call[1])["kind"] for call in recorder.calls] == ["message.created"]
    archive = factory()
    try:
        # 걸러낸 이벤트를 지나서도 커서는 계속 나아간다.
        assert archive.get_webhook("hook-1")["cursor_seq"] == archive.latest_seq()
    finally:
        archive.close()


def test_failure_keeps_the_cursor_and_records_the_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    factory = _archive(settings, events=1)
    recorder = Recorder([DeliveryResult(False, "ConnectionRefusedError")])
    dispatcher = WebhookDispatcher(settings, factory, EventBus(), sender=recorder)

    delivered = dispatcher.dispatch_once(now=100.0)

    assert delivered == 0
    archive = factory()
    try:
        hook = archive.get_webhook("hook-1")
        assert hook["cursor_seq"] == 0
        assert hook["last_error"] == "ConnectionRefusedError"
    finally:
        archive.close()

    # 백오프가 지나기 전의 재시도는 sender를 다시 부르면 안 된다.
    assert dispatcher.dispatch_once(now=100.5) == 0
    assert len(recorder.calls) == 1


def test_retry_after_backoff_succeeds(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    factory = _archive(settings, events=1)
    recorder = Recorder([DeliveryResult(False, "boom"), DeliveryResult(True)])
    dispatcher = WebhookDispatcher(settings, factory, EventBus(), sender=recorder)

    dispatcher.dispatch_once(now=100.0)
    dispatcher.dispatch_once(now=1_000.0)

    assert len(recorder.calls) == 2
    archive = factory()
    try:
        # 커서는 전달된 이벤트와 그 뒤의 걸러낸 이벤트를 모두 지나간다.
        assert archive.get_webhook("hook-1")["cursor_seq"] == archive.latest_seq()
    finally:
        archive.close()


def test_poisoned_event_is_skipped_after_max_attempts(tmp_path: Path) -> None:
    settings = _settings(tmp_path, webhook_max_attempts=2)
    factory = _archive(settings, events=1)
    recorder = Recorder([DeliveryResult(False, "boom"), DeliveryResult(False, "boom")])
    dispatcher = WebhookDispatcher(settings, factory, EventBus(), sender=recorder)

    dispatcher.dispatch_once(now=100.0)
    dispatcher.dispatch_once(now=1_000.0)

    assert len(recorder.calls) == 2
    archive = factory()
    try:
        assert archive.get_webhook("hook-1")["cursor_seq"] == 1
        assert archive.get_webhook("hook-1")["last_error"] == "boom"
    finally:
        archive.close()


def test_inactive_webhooks_are_not_called(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    factory = _archive(settings, events=1, active=False)
    recorder = Recorder()
    dispatcher = WebhookDispatcher(settings, factory, EventBus(), sender=recorder)

    assert dispatcher.dispatch_once(now=time.time()) == 0
    assert recorder.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8757/hook",
        "http://localhost:8757/hook",
        "http://[::1]:8757/hook",
    ],
)
def test_valid_loopback_urls(url: str) -> None:
    assert validate_webhook_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "https://127.0.0.1:8757/hook",
        "http://127.0.0.1/hook",
        "",
        None,
        123,
    ],
)
def test_invalid_webhook_urls(url) -> None:
    with pytest.raises(ApiError):
        validate_webhook_url(url)
