"""웹훅 전달 워커.

각 구독의 대기 중인 이벤트를 아카이브에서 읽어 로컬 대상에 POST하고, 본문에 HMAC-SHA256
서명을 붙인다. 전달은 최소 1회 보장이다: 커서는 2xx를 받은 뒤에만 나아가고, 실패하면
``SESAMETOK_WEBHOOK_MAX_ATTEMPTS``까지 지수 백오프한 뒤 그 이벤트를 건너뛰고 ``last_error``에
기록한다. 그래야 망가진 엔드포인트 하나가 구독을 영원히 막지 못한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..config import Settings
from ..core.archive import Archive
from ..core.events import EventBus
from ..models import Event
from .errors import bad_request

logger = logging.getLogger("sesametok_backend.api.webhooks")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
SIGNATURE_HEADER = "X-Sesametok-Signature"
SEQUENCE_HEADER = "X-Sesametok-Event-Seq"
KIND_HEADER = "X-Sesametok-Event-Kind"
BATCH = 50
IDLE_TIMEOUT = 1.0


def validate_webhook_url(url: object) -> str:
    """루프백 ``http`` 전용 — 이 백엔드는 네트워크로 나가지 않는다."""

    if not isinstance(url, str) or not url.strip():
        raise bad_request("webhook URL이 필요합니다")
    parts = urlsplit(url.strip())
    if parts.scheme != "http":
        raise bad_request("webhook URL은 http://를 써야 합니다(루프백 대상만 허용)")
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise bad_request(
            "webhook URL은 루프백(127.0.0.1, ::1, localhost)을 가리켜야 합니다; "
            "외부 메신저로의 전달은 인터페이스 프로젝트의 책임입니다"
        )
    if parts.port is None:
        raise bad_request("webhook URL에는 포트를 명시해야 합니다")
    return url.strip()


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def encode_event(event: Event) -> bytes:
    return json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8")


@dataclass
class DeliveryResult:
    ok: bool
    error: str | None = None


def default_sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> DeliveryResult:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        return DeliveryResult(False, f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return DeliveryResult(False, type(exc).__name__)
    if 200 <= status < 300:
        return DeliveryResult(True)
    return DeliveryResult(False, f"HTTP {status}")


Sender = Callable[[str, bytes, dict[str, str], float], DeliveryResult]


class WebhookDispatcher(threading.Thread):
    """아카이브된 이벤트를 로컬 구독자에게 전달하며, 각자의 커서에서 이어받는다."""

    def __init__(
        self,
        settings: Settings,
        archive_factory: Callable[[], Archive],
        bus: EventBus,
        *,
        sender: Sender = default_sender,
    ) -> None:
        super().__init__(name="sesametok-webhooks", daemon=True)
        self.settings = settings
        self.archive_factory = archive_factory
        self.bus = bus
        self.sender = sender
        self.deliveries = 0
        self.failures = 0
        self._stop = threading.Event()
        self._next_attempt: dict[str, float] = {}
        self._attempts: dict[str, int] = {}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        subscription = self.bus.subscribe()
        try:
            while not self._stop.is_set():
                subscription.get(timeout=IDLE_TIMEOUT)
                self.dispatch_once()
        finally:
            self.bus.unsubscribe(subscription)

    # ------------------------------------------------------------- 전달

    def dispatch_once(self, *, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        archive = self.archive_factory()
        sent = 0
        try:
            hooks = archive.list_webhooks(include_secret=True)
            for hook in hooks:
                if not hook["active"]:
                    continue
                if self._next_attempt.get(hook["id"], 0.0) > moment:
                    continue
                sent += self._deliver_hook(archive, hook, moment)
            archive.commit()
        finally:
            archive.close()
        return sent

    def _deliver_hook(self, archive: Archive, hook: dict, moment: float) -> int:
        kinds = set(hook["kinds"])
        pending = archive.events_since(int(hook["cursor_seq"]), limit=BATCH)
        sent = 0
        for event in pending:
            if kinds and event.kind not in kinds:
                archive.set_webhook_cursor(hook["id"], event.seq)
                continue

            body = encode_event(event)
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "sesametok-backend",
                SIGNATURE_HEADER: sign(hook["secret"], body),
                SEQUENCE_HEADER: str(event.seq),
                KIND_HEADER: event.kind,
            }
            result = self.sender(hook["url"], body, headers, self.settings.webhook_timeout_seconds)

            if result.ok:
                archive.set_webhook_cursor(hook["id"], event.seq)
                archive.record_webhook_result(hook["id"], success=True)
                self._attempts[hook["id"]] = 0
                self.deliveries += 1
                sent += 1
                continue

            self.failures += 1
            attempts = self._attempts.get(hook["id"], 0) + 1
            self._attempts[hook["id"]] = attempts
            archive.record_webhook_result(
                hook["id"], success=False, error=result.error or "전달 실패"
            )
            logger.warning(
                "웹훅 %s 전달 실패(%d번째 시도): %s",
                hook["id"],
                attempts,
                result.error,
            )
            if attempts >= max(1, self.settings.webhook_max_attempts):
                # 구독이 계속 나아가도록 오염된 이벤트를 건너뛴다.
                archive.set_webhook_cursor(hook["id"], event.seq)
                self._attempts[hook["id"]] = 0
                self._next_attempt[hook["id"]] = moment + 60.0
            else:
                self._next_attempt[hook["id"]] = moment + min(2.0**attempts, 60.0)
            break
        return sent
