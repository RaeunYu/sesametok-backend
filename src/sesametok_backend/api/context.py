"""모든 API 라우트에 전달되는 공유 상태.

API는 ``Runtime``을 import하지 않는다(런타임이 API를 import한다). 그래서 살아 있는 상태는
콜러블을 통해 들어온다. SQLite 연결은 스레드에 귀속되므로 아카이브 연결은 요청마다 새로 내준다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Settings
from ..core.archive import Archive
from ..core.events import EventBus
from ..core.poller import Poller
from ..core.send_queue import SendQueue
from .auth import authorize

HealthProvider = Callable[[], dict]
StatusProvider = Callable[[], dict]


@dataclass
class ApiContext:
    settings: Settings
    token: str
    archive_factory: Callable[[], Archive]
    bus: EventBus
    poller: Poller | None = None
    send_queue: SendQueue | None = None
    health_provider: HealthProvider | None = None
    status_provider: StatusProvider | None = None
    webhook_count: Callable[[], dict] | None = None
    shutdown: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)

    def require_auth(self, request) -> None:
        authorize(self.token, request.header("Authorization"))

    def open_archive(self) -> Archive:
        return self.archive_factory()

    def health_payload(self) -> dict:
        base = self.health_provider() if self.health_provider else {}
        failures = self.poller.stats.consecutive_failures if self.poller else 0
        return {
            "ok": bool(base.get("running")) and failures == 0,
            "kakaotalk_running": base.get("kakaotalk_running"),
            "permissions_ok": base.get("permissions_ok"),
            "polling": base.get("polling"),
            "last_sync_at": base.get("last_sync_at"),
            "consecutive_failures": failures,
        }

    def status_payload(self) -> dict:
        archive = self.open_archive()
        try:
            counts = {
                "rooms": archive.count_rooms(),
                "messages": archive.count_messages(),
                "event_seq": archive.latest_seq(),
                "oldest_event_seq": archive.oldest_seq(),
            }
        finally:
            archive.close()

        payload: dict = {
            "uptime_seconds": int(time.time() - self.started_at),
            "archive": {**counts, "path": str(self.settings.archive_path)},
            "polling": self.poller.stats.as_dict() if self.poller else None,
            "webhooks": self.webhook_count() if self.webhook_count else {"total": 0, "active": 0},
        }
        if self.status_provider:
            payload.update(self.status_provider())
        return payload
