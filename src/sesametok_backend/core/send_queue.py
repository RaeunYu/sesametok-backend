"""발신 워커(스레드 C): 강한 게이트 뒤에서 한 번에 메시지 하나.

발신은 되돌릴 수 없으므로 이 모듈은 워커라기보다 게이트에 가깝다.

* ``send``/``draft`` 에는 동의가 필요하다(``dry_run`` 은 예외),
* ``client_request_id`` 가 재시도를 멱등하게 만든다,
* 단일 워커가 발신을 직렬화하여 둘이 카카오톡 UI에서 경합하지 않게 한다,
* 분당 상한이 자동화 범위를 제한한다,
* 결과는 본문 없이 ``send.result`` 로 기록되고 발행된다.

실제 UI 구동은 :class:`MessageSender` 뒤에 있으므로, 화면 없이 게이트를
테스트할 수 있다.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..core.archive import Archive
from ..core.events import EventBus
from ..models import Room
from ..send.errors import SendError
from ..send.targets import SendTarget, TargetError, resolve_target

logger = logging.getLogger("sesametok_backend.core.send_queue")

VALID_MODES = ("send", "draft", "dry_run")
RATE_WINDOW_SECONDS = 60.0
DEFAULT_WAIT_SECONDS = 90.0
TERMINAL_RESULTS = frozenset({"sent", "drafted", "unconfirmed"})


@dataclass(frozen=True)
class SendRequest:
    mode: str = "send"
    chat_id: str | None = None
    text: str | None = None
    image_path: Path | None = None
    background_only: bool = False
    accept_use_policy: bool = False
    client_request_id: str | None = None


@dataclass(frozen=True)
class SendOutcome:
    """UI 계층이 관찰한 결과."""

    result: str  # sent | drafted | unconfirmed
    code: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SendResult:
    result: str
    room: str | None = None
    chars: int = 0
    client_request_id: str | None = None
    code: str | None = None
    message: str | None = None
    target: SendTarget | None = None

    def to_dict(self) -> dict:
        payload: dict = {
            "result": self.result,
            "room": self.room,
            "chars": self.chars,
            "client_request_id": self.client_request_id,
        }
        if self.code:
            payload["error"] = {"code": self.code, "message": self.message, "retryable": False}
        return payload


class MessageSender(Protocol):
    def send_text(
        self, target: SendTarget, text: str, *, mode: str, background_only: bool
    ) -> SendOutcome: ...

    def send_image(self, target: SendTarget, path: Path, *, background_only: bool) -> SendOutcome: ...


@dataclass
class SendStats:
    queued: int = 0
    completed: int = 0
    refused: int = 0
    failed: int = 0
    last_result: str | None = None
    last_error: str | None = None
    in_flight: bool = False

    def as_dict(self) -> dict:
        return {
            "queued": self.queued,
            "completed": self.completed,
            "refused": self.refused,
            "failed": self.failed,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "in_flight": self.in_flight,
        }


@dataclass
class _Job:
    request: SendRequest
    future: "queue.Queue[SendResult]" = field(default_factory=queue.Queue)


class SendQueue(threading.Thread):
    def __init__(
        self,
        settings: Settings,
        archive_factory: Callable[[], Archive],
        bus: EventBus,
        sender: MessageSender,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(name="sesametok-send", daemon=True)
        self.settings = settings
        self.archive_factory = archive_factory
        self.bus = bus
        self.sender = sender
        self.stats = SendStats()
        self._clock = clock
        self._jobs: "queue.Queue[_Job]" = queue.Queue()
        self._stop = threading.Event()
        self._recent: deque[float] = deque()
        self._recent_by_room: dict[str, deque[float]] = {}

    # ------------------------------------------------------------- 생명주기

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                result = self.process(job.request)
            except Exception as exc:  # 작업 하나가 워커를 죽이게 두지 않는다
                logger.error("발신 워커 오류: %s", type(exc).__name__)
                result = SendResult(
                    result="failed", code="internal_error", message=type(exc).__name__
                )
            job.future.put(result)

    def submit(self, request: SendRequest, *, timeout: float = DEFAULT_WAIT_SECONDS) -> SendResult:
        """요청을 큐에 넣고 결과를 기다린다(설계상 동시 처리 한 건)."""

        if not self.is_alive():
            self.start()
        job = _Job(request=request)
        self.stats.queued += 1
        self._jobs.put(job)
        try:
            return job.future.get(timeout=timeout)
        except queue.Empty:
            raise SendError("unconfirmed", retryable=False)

    # ---------------------------------------------------------------- 게이트

    def _check_consent(self, request: SendRequest) -> None:
        if request.mode in ("send", "draft") and not request.accept_use_policy:
            raise SendError("no_consent")

    def _validate_request(self, request: SendRequest) -> None:
        if request.mode not in VALID_MODES:
            raise SendError("bad_mode", f"mode는 {', '.join(VALID_MODES)} 중 하나여야 한다")
        if not request.chat_id:
            raise SendError("bad_request", "chat_id는 필수다")
        has_text = bool(request.text)
        has_image = request.image_path is not None
        if request.mode == "send" and not has_text and not has_image:
            raise SendError("bad_request", "text 또는 image_path가 필요하다")
        if has_text and has_image:
            raise SendError("bad_request", "텍스트와 이미지 중 하나만 보내야 한다")
        if has_text and request.text and len(request.text) > 20_000:
            raise SendError("bad_request", "텍스트가 너무 길다")

    def _check_rate_limit(self, request: SendRequest) -> None:
        if request.mode == "dry_run":
            return
        now = self._clock()
        cutoff = now - RATE_WINDOW_SECONDS

        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        room_bucket = self._recent_by_room.setdefault(str(request.chat_id), deque())
        while room_bucket and room_bucket[0] < cutoff:
            room_bucket.popleft()

        if len(self._recent) >= self.settings.max_sends_per_minute:
            raise SendError("rate_limited", "전역 발신 레이트 리밋에 도달했다")
        if len(room_bucket) >= self.settings.max_sends_per_room_per_minute:
            raise SendError("rate_limited", "방별 발신 레이트 리밋에 도달했다")

    def _record_rate(self, request: SendRequest) -> None:
        if request.mode == "dry_run":
            return
        now = self._clock()
        self._recent.append(now)
        self._recent_by_room.setdefault(str(request.chat_id), deque()).append(now)

    # -------------------------------------------------------- 멱등성

    def _lookup_previous(self, archive: Archive, request: SendRequest) -> SendResult | None:
        if not request.client_request_id:
            return None
        record = archive.get_send_request(request.client_request_id)
        if record is None:
            return None
        stored = record["result"]
        if stored not in TERMINAL_RESULTS:
            # 이전 시도가 결과를 보고하지 않았다. 재시도하면 중복 발신이 될 수
            # 있으므로 추측하지 않고 그대로 알린다.
            raise SendError("unconfirmed")
        return SendResult(
            result=stored,
            room=None,
            chars=0,
            client_request_id=record["client_request_id"],
            code="duplicate_request",
            message="이 client_request_id는 이미 처리되었으므로 저장된 결과를 반환한다",
        )

    # -------------------------------------------------------------- 처리

    def _rooms(self, archive: Archive) -> list[Room]:
        return archive.rooms()

    def process(self, request: SendRequest) -> SendResult:
        self._validate_request(request)
        self._check_consent(request)

        archive = self.archive_factory()
        try:
            previous = self._lookup_previous(archive, request)
            if previous is not None:
                return previous

            try:
                target = resolve_target(self._rooms(archive), str(request.chat_id))
            except TargetError as exc:
                raise SendError(exc.code, **exc.details) from exc

            if request.mode == "dry_run":
                self.stats.completed += 1
                self.stats.last_result = "dry_run"
                return SendResult(
                    result="dry_run",
                    room=target.room_name,
                    chars=len(request.text or ""),
                    client_request_id=request.client_request_id,
                    target=target,
                )

            self._check_rate_limit(request)
            if request.client_request_id:
                archive.record_send_request(
                    client_request_id=request.client_request_id,
                    chat_id=str(request.chat_id),
                    mode=request.mode,
                    result="pending",
                )
                archive.commit()

            self.stats.in_flight = True
            try:
                outcome = self._dispatch(request, target)
            finally:
                self.stats.in_flight = False

            self._record_rate(request)
            result = SendResult(
                result=outcome.result,
                room=target.room_name,
                chars=len(request.text or ""),
                client_request_id=request.client_request_id,
                code=outcome.code,
                message=outcome.detail,
                target=target,
            )
            self._finalise(archive, request, result)
            self.stats.completed += 1
            self.stats.last_result = outcome.result
            if outcome.result == "unconfirmed":
                logger.warning("방으로의 발신이 unconfirmed로 보고되었다. 재시도하지 않는다")
            return result
        finally:
            archive.close()

    def _dispatch(self, request: SendRequest, target: SendTarget) -> SendOutcome:
        if request.image_path is not None:
            return self.sender.send_image(
                target, request.image_path, background_only=request.background_only
            )
        return self.sender.send_text(
            target,
            request.text or "",
            mode=request.mode,
            background_only=request.background_only,
        )

    def _finalise(self, archive: Archive, request: SendRequest, result: SendResult) -> None:
        if request.client_request_id:
            archive.complete_send_request(request.client_request_id, result.result)
        event = archive.append_event(
            "send.result",
            {
                "client_request_id": request.client_request_id,
                "chat_id": str(request.chat_id),
                "mode": request.mode,
                "result": result.result,
                "chars": result.chars,
                "code": result.code,
            },
        )
        archive.commit()
        self.bus.publish(event)
