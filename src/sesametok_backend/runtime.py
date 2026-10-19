"""단일 백엔드 프로세스.

``Runtime``은 프로세스 수준의 관심사를 소유하고 파이프라인을 조립한다:

    식별 정보 + 키  →  암호화 아카이브  →  폴러(스레드 B)  →  이벤트 버스
                                              ↓
                                   웹훅, 발신 워커, API

종료는 순서가 있고 멱등적이다: 훅이 등록의 역순으로 실행되므로 API가 먼저 멈추고,
그다음 발신 워커가 비워지고, 그다음 웹훅이 멈추며, 폴러가 마지막에 멈춘다.
``settings.api_enabled``가 false(``SESAMETOK_API=0``)이면 폴러를 제외한 모든
서비스를 건너뛰며, 테스트와 헤드리스 CI가 이렇게 실행된다.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import diagnostics
from .config import Settings
from .core import preferences
from .core.archive import Archive
from .core.events import EventBus
from .core.poller import Poller, PollSource
from .kakao import store
from .kakao.auth import AccountIdentity, resolve_identity
from .kakao.derive import account_hash as derive_account_hash
from .kakao.source import KakaoSource
from .launcher import ProcessLock
from .security.archive_key import (
    KEY_BACKUP_HELP,
    ArchiveKey,
    ArchiveKeyError,
    key_to_hex,
    resolve_archive_key,
)
from .security.tokens import load_or_create_token

if TYPE_CHECKING:  # pragma: no cover - 타입 검사 전용이며, API를 미리 임포트하지 않는다
    from .api.server import ApiService
    from .api.webhooks import WebhookDispatcher
    from .core.send_queue import SendQueue

logger = logging.getLogger("sesametok_backend.runtime")


class AlreadyRunningError(RuntimeError):
    """다른 살아 있는 백엔드 프로세스가 인스턴스 잠금을 쥐고 있을 때 발생한다."""


class StartupError(RuntimeError):
    """백엔드가 시작할 수 없을 때(식별 정보, 키, 아카이브) 발생한다."""


@dataclass
class Runtime:
    settings: Settings
    foreground: bool = True
    managed: bool = False
    """launchd가 실행한 경우 True이며, 이때 stderr는 로그 파일이 된다."""

    interactive: bool | None = None
    api_token: str | None = None
    diagnostic_report: dict = field(default_factory=dict)
    identity: AccountIdentity | None = None
    account_hash: str | None = None
    archive_key_source: str | None = None
    # 주입 가능한 이음매: 프로덕션은 실제 카카오톡 소스와 리졸버를 만들고,
    # 테스트는 조용한 것으로 대체하므로 어떤 테스트도 실제 설치를 건드리지 않는다.
    source: PollSource | None = None
    identity_resolver: Callable[[Settings], AccountIdentity] | None = None
    archive_key_resolver: Callable[[Settings], ArchiveKey] | None = None
    _lock: ProcessLock = field(init=False)
    _stop: threading.Event = field(init=False, default_factory=threading.Event)
    _started: bool = field(init=False, default=False)
    _shutdown_hooks: list[Callable[[], None]] = field(init=False, default_factory=list)
    # 동시 shutdown() 호출을 직렬화한다: 메뉴 바가 백그라운드 스레드에서 실행 루프를
    # 멈추는 동안 run_foreground의 finally도 shutdown()을 호출할 수 있다.
    _shutdown_lock: threading.RLock = field(init=False, default_factory=threading.RLock)
    _archive_key: bytes | None = field(init=False, default=None)
    _bus: EventBus = field(init=False, default_factory=EventBus)
    _poller: Poller | None = field(init=False, default=None)
    _webhooks: WebhookDispatcher | None = field(init=False, default=None)
    _send_queue: SendQueue | None = field(init=False, default=None)
    _api: ApiService | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._lock = ProcessLock(self.settings.lock_path)
        if self.interactive is None:
            self.interactive = bool(sys.stderr.isatty()) and not self.managed

    # -------------------------------------------------------------------- 수명주기

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        self._shutdown_hooks.append(hook)

    @property
    def events(self) -> EventBus:
        return self._bus

    @property
    def poller(self) -> Poller | None:
        return self._poller

    @property
    def api(self) -> ApiService | None:
        return self._api

    @property
    def send_queue(self) -> SendQueue | None:
        return self._send_queue

    @property
    def sending(self) -> bool:
        """발신 하나가 진행 중인 동안 True (``Status.sending``을 구동한다)."""

        return bool(self._send_queue is not None and self._send_queue.stats.in_flight)

    def archive_factory(self) -> Callable[[], Archive]:
        """스레드별 읽기 연결을 위한 팩터리 (API 워커)."""

        def factory() -> Archive:
            if self._archive_key is None or self.account_hash is None:
                raise StartupError("아카이브가 준비되지 않았습니다")
            return Archive.open(
                self.settings,
                self._archive_key,
                account_hash=self.account_hash,
                initialize=False,
            )

        return factory

    def start(self) -> None:
        if self._started:
            return

        self.settings.ensure_directories()
        if not self._lock.acquire():
            owner = self._lock.owner_pid()
            detail = f" (pid {owner})" if owner else ""
            raise AlreadyRunningError(f"다른 sesametok-backend가 이미 실행 중입니다{detail}")

        try:
            self.api_token = load_or_create_token(self.settings)
            self.diagnostic_report = diagnostics.collect(self.settings, store.store_roots())
            self._load_archive_key()
            self._resolve_identity()
            self._initialize_archive()
            self._start_poller()
            if self.settings.api_enabled:
                self._start_webhooks()
                self._start_send_worker()
                self._start_api()
        except Exception:
            self._lock.release()
            raise

        self._started = True
        logger.info("sesametok-backend %s 시작 중", _version())
        logger.info("설정 %s", self.settings.redacted())
        logger.info("계정 출처 %s; 아카이브 키 출처 %s",
                    self.identity.source if self.identity else "?",
                    self.archive_key_source)

        for line in diagnostics.permission_guidance(self.diagnostic_report):
            for part in line.splitlines():
                logger.warning("%s", part)

    def _load_archive_key(self) -> None:
        resolver = self.archive_key_resolver or resolve_archive_key
        try:
            resolved: ArchiveKey = resolver(self.settings)
        except ArchiveKeyError as exc:
            raise StartupError(
                f"{exc}\nSESAMETOK_ARCHIVE_KEY를 설정하거나 로그인 키체인 접근을 "
                "허용한 뒤 다시 시도하세요."
            ) from exc

        self._archive_key = resolved.key
        self.archive_key_source = resolved.source
        if resolved.is_new:
            self._announce_new_archive_key(resolved)

    def _announce_new_archive_key(self, resolved: ArchiveKey) -> None:
        message = [
            "새 아카이브 키를 만들어 로그인 키체인에 저장했습니다.",
            KEY_BACKUP_HELP,
        ]
        for line in message:
            logger.warning("%s", line)
        # stderr가 launchd 로그 파일일 때는 키를 절대 출력하지 않는다.
        if self.interactive:
            print(
                "\n  아카이브 키: "
                + key_to_hex(resolved.key)
                + "\n  지금 저장하세요; 대화형 첫 실행이기 때문에 여기서만 "
                "보여줍니다.\n",
                file=sys.stderr,
            )

    def _resolve_identity(self) -> None:
        try:
            if self.identity_resolver is not None:
                identity = self.identity_resolver(self.settings)
            else:
                identity = resolve_identity(
                    self.settings,
                    allow_reverse_lookup=True,
                    should_stop=lambda: self._stop.is_set(),
                )
        except Exception as exc:
            raise StartupError(
                "카카오톡 계정을 확인하지 못했습니다: "
                f"{exc}\nSESAMETOK_USER_ID를 계정의 숫자 user id로 설정하고 "
                "다시 시도하세요."
            ) from exc
        self.identity = identity
        self.account_hash = derive_account_hash(identity.user_id)

    def _initialize_archive(self) -> None:
        assert self._archive_key is not None and self.account_hash is not None
        archive = Archive.open(
            self.settings, self._archive_key, account_hash=self.account_hash, initialize=True
        )
        try:
            logger.info(
                "아카이브 준비 완료: 방 %d개, 메시지 %d개, 이벤트 %d개",
                archive.count_rooms(),
                archive.count_messages(),
                archive.latest_seq(),
            )
        finally:
            archive.close()

    def _start_poller(self) -> None:
        assert self.identity is not None
        source = self.source or KakaoSource(identity=self.identity, roots=store.store_roots())
        self._poller = Poller(
            self.settings,
            self.archive_factory(),
            self._bus,
            source,
            interval=self.poll_interval(),
        )
        self._poller.start()
        self.add_shutdown_hook(self._stop_poller)

    def _stop_poller(self) -> None:
        if self._poller is None:
            return
        self._poller.stop()
        self._poller.join(timeout=10)
        self._poller = None

    # ----------------------------------------------------------------- 웹훅 / 발신

    def _start_webhooks(self) -> None:
        from .api.webhooks import WebhookDispatcher

        self._webhooks = WebhookDispatcher(self.settings, self.archive_factory(), self._bus)
        self._webhooks.start()
        self.add_shutdown_hook(self._stop_webhooks)

    def _stop_webhooks(self) -> None:
        if self._webhooks is None:
            return
        self._webhooks.stop()
        self._webhooks.join(timeout=10)
        self._webhooks = None

    def _start_send_worker(self) -> None:
        """발신 워커를 만들되, UI 드라이버가 없어도 시작을 막지 않는다.

        ``SendQueue``는 첫 ``submit()``에서 지연 시작한다; 손쉬운 사용 드라이버는
        프레임워크를 쓸 수 없을 때 빨리 실패하도록 미리 만들 뿐이고, 실패하면
        발신이 비활성화될 뿐이다.
        """

        from .core.send_queue import SendQueue

        try:
            from .send.accessibility import KakaoTalkDriver
            from .send.curtain import Curtain

            sender = KakaoTalkDriver(curtain=Curtain())
        except Exception as exc:
            logger.warning("발신 비활성화됨: %s", type(exc).__name__)
            return
        self._send_queue = SendQueue(self.settings, self.archive_factory(), self._bus, sender)
        self.add_shutdown_hook(self._stop_send_worker)

    def _stop_send_worker(self) -> None:
        queue = self._send_queue
        if queue is None:
            return
        queue.stop()
        # 시작한 적이 없으면 join하지 않는다: SendQueue는 첫 submit()에서 시작한다.
        if queue.is_alive():
            queue.join(timeout=10)
        self._send_queue = None

    # ------------------------------------------------------------------- API

    def _start_api(self) -> None:
        from .api.context import ApiContext
        from .api.server import build_api_service

        assert self.api_token is not None
        context = ApiContext(
            settings=self.settings,
            token=self.api_token,
            archive_factory=self.archive_factory(),
            bus=self._bus,
            poller=self._poller,
            send_queue=self._send_queue,
            health_provider=self.health,
            status_provider=self._status_extra,
            webhook_count=self._webhook_count,
        )
        self._api = build_api_service(context)
        self._api.start()
        self.add_shutdown_hook(self._stop_api)

    def _stop_api(self) -> None:
        if self._api is None:
            return
        self._api.stop()
        self._api = None

    def _status_extra(self) -> dict:
        """``GET /v1/status``에 추가되는 필드이며, 민감 정보를 제거했다 (id, 키, 토큰 없음)."""

        identity = self.identity
        account: dict[str, object] = {"source": identity.source if identity else None}
        if identity is not None:
            account["user_id_digits"] = len(str(identity.user_id))
            account["uuid_shape"] = "36-chars"
        return {
            "version": _version(),
            "account": account,
            "permissions": self.diagnostic_report,
        }

    def _webhook_count(self) -> dict:
        """``/v1/status``용 구독 개수이며, URL이나 비밀 값은 없다."""

        try:
            archive = self.archive_factory()()
        except Exception:
            return {"total": 0, "active": 0}
        try:
            hooks = archive.list_webhooks(include_secret=False)
        finally:
            archive.close()
        return {"total": len(hooks), "active": sum(1 for hook in hooks if hook.get("active"))}

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._stop.wait(timeout)

    def shutdown(self) -> None:
        """등록의 역순으로 모든 서비스를 한 번씩 멈춘다.

        멱등적이고 어느 스레드에서 호출해도 안전하다; 두 번째 동시 호출자는 첫
        정리가 끝나기를 기다린 뒤 아무것도 하지 않는다.
        """

        with self._shutdown_lock:
            self._stop.set()
            hooks = list(reversed(self._shutdown_hooks))
            self._shutdown_hooks.clear()
            for hook in hooks:
                try:
                    hook()
                except Exception:
                    logger.exception("종료 훅이 실패했습니다")
            if self._started:
                logger.info("sesametok-backend가 중지되었습니다")
            self._lock.release()
            self._started = False

    # -------------------------------------------------------------------- 실행기

    def run_foreground(self, install_signal_handlers: bool = True) -> int:
        # 핸들러를 먼저 설치한다: 느린 첫 실행 식별 조회(역방향 검색은 ~60초 걸릴 수
        # 있다)도 여전히 중단할 수 있어야 하고, 시작 중의 시그널이 반쯤 초기화된
        # 아카이브를 남겨서는 안 된다.
        if install_signal_handlers:
            self._install_signal_handlers()
        self.start()
        if self.managed:
            # 앱 스텁이 launchd에게 SIGTERM을 받아 죽으면 python 자식은 고아가 되어
            # 계속 살아남는다(bootout이 main 프로세스만 정지시킨다). 실측으로 확인한
            # 결함이라, 부모가 사라지면 스스로 종료한다.
            threading.Thread(
                target=self._watch_parent,
                name="sesametok-parent-watch",
                daemon=True,
            ).start()
        try:
            if self._menubar_enabled():
                self._run_menubar()
            else:
                self._wait_plain()
        except KeyboardInterrupt:
            logger.info("중단되었습니다")
        finally:
            self.shutdown()
        return 0

    def _watch_parent(self, interval: float = 1.0) -> None:
        """launchd 관리 모드에서 부모(앱 스텁)가 사라지면 스스로 종료한다.

        launchd의 ``bootout``은 잡의 main 프로세스(앱 스텁)만 정지시킨다. 스텁이 죽으면
        우리는 ``launchd``가 아니라 PID 1에 입양되므로, 그때 스스로 내려간다. 이 장치가
        없으면 ``stop`` 이후에도 API와 폴러가 계속 살아 있고 ``start``는 락 때문에
        "이미 실행 중"으로 복구를 거부한다.
        """

        while not self._stop.wait(interval):
            if os.getppid() != 1:
                continue
            logger.warning("부모 프로세스가 사라졌습니다. 백엔드를 종료합니다.")
            self.request_stop()
            try:
                self.shutdown()
            except Exception:  # 종료 경로의 실패가 강제 종료를 막아서는 안 된다
                logger.exception("부모 감시 종료 중 오류")
            os._exit(0)

    def _menubar_enabled(self) -> bool:
        """이 프로세스가 AppKit 메뉴 바를 소유해야 하면 True.

        AppKit은 메인 스레드 전용이며, 테스트/CI는 ``SESAMETOK_NO_MENUBAR=1``로
        헤드리스 실행한다.
        """

        return (
            not self.settings.no_menubar
            and threading.current_thread() is threading.main_thread()
        )

    def _run_menubar(self) -> None:
        """PyObjC 메뉴 바 실행 루프를 돌린다 (AppKit이 없으면 아무것도 하지 않고 기다린다)."""

        try:
            from .app.menubar import run_menubar
        except ImportError as exc:
            logger.error("메뉴 바를 쓸 수 없습니다 (%s); 헤드리스로 실행합니다", exc)
            self._wait_plain()
            return
        run_menubar(
            self,
            quit_callback=self._menu_quit,
            restart_callback=self._menu_restart,
            autostart_provider=self.autostart_enabled,
            toggle_autostart=self.toggle_autostart,
            restart_provider=self.restart_enabled,
            interval_provider=self.poll_interval,
            set_interval=self.set_poll_interval,
        )

    def autostart_enabled(self) -> bool:
        """이 사용자에게 LaunchAgent가 설치되어 있으면 True.

        메뉴 체크박스는 plist의 존재를 반영하며, 로드된 작업이 지금 실행 중인지는
        반영하지 않는다.
        """

        try:
            from .launcher import LaunchAgent, plist_path_is_allowed

            agent = LaunchAgent(self.settings)
            if not plist_path_is_allowed(agent.plist_path):
                return False
            return agent.plist_path.exists()
        except Exception:
            logger.debug("로그인 자동 시작 상태를 읽지 못했습니다", exc_info=True)
            return False

    def toggle_autostart(self) -> bool | None:
        """LaunchAgent를 설치하거나 제거한다. 새 상태 또는 ``None``을 반환한다.

        비활성화는 plist만 제거한다: 실행 중인 프로세스는 건드리지 않으며(명시적으로
        시작한 것이므로), 다음 로그인에 다시 살아나지 않을 뿐이다.
        """

        try:
            from .launcher import (
                LaunchAgent,
                PlistPathNotAllowed,
                require_plist_path_allowed,
            )

            agent = LaunchAgent(self.settings)
            require_plist_path_allowed(agent.plist_path)
        except PlistPathNotAllowed:
            logger.warning("이 plist 경로에서는 로그인 자동 시작을 쓸 수 없습니다")
            return None
        except Exception:
            logger.debug("로그인 자동 시작을 쓸 수 없습니다", exc_info=True)
            return None

        try:
            if agent.plist_path.exists():
                agent.remove_plist()
                logger.info(
                    "로그인 자동 시작을 껐습니다; 이 프로세스는 로그아웃까지 계속 실행됩니다"
                )
                return False

            agent.install_plist()
            result = agent.bootstrap()
            if not result.ok:
                logger.warning("launchctl bootstrap에 실패했습니다: %s", result.message)
                agent.remove_plist()
                return None
            logger.info("로그인 자동 시작을 켰습니다")
            return True
        except Exception as exc:
            logger.warning("로그인 자동 시작 전환에 실패했습니다: %s", type(exc).__name__)
            return None

    # ------------------------------------------------------- 폴링 주기(메뉴 연동)

    def preferences_store(self) -> preferences.Preferences:
        """메뉴바 선택을 기억하는 작은 JSON 저장소(``<data_dir>/preferences.json``).

        주기 숫자만 담으며 대화 데이터는 절대 넣지 않는다.
        """

        return preferences.Preferences.for_data_dir(self.settings.data_dir)

    def poll_interval(self) -> float:
        """지금 적용할 폴링 주기(초).

        우선순위: 저장된 선택 > ``SESAMETOK_POLL_SECONDS`` > 기본 3초. 저장값이
        0 이하이거나 비정상이면 무시한다. 폴러가 살아 있으면 그 실제 주기를
        돌려주므로 메뉴 체크 표시가 항상 현재 상태와 맞는다. 메뉴바가 없어도
        (``SESAMETOK_NO_MENUBAR=1``) 이 값이 폴러에 쓰인다.
        """

        poller = self._poller
        if poller is not None:
            return poller.interval
        return preferences.resolve_poll_seconds(
            self.preferences_store().poll_seconds(),
            self.settings.poll_seconds,
        )

    def set_poll_interval(self, seconds: float) -> bool:
        """폴링 주기를 즉시 바꾸고 다음 실행을 위해 저장한다.

        재시작이 필요 없다: 폴러의 대기 주기가 곧바로 새 값으로 다시 계산된다.
        저장에 실패해도 이번 실행에는 계속 적용되며(로그만 남긴다), 유효하지 않은
        값이면 아무것도 바꾸지 않고 ``False``를 반환한다.
        """

        value = preferences.coerce_poll_seconds(seconds)
        if value is None:
            logger.warning("폴링 주기가 유효하지 않아 바꾸지 않았습니다")
            return False
        poller = self._poller
        if poller is not None:
            try:
                poller.set_interval(value)
            except ValueError:
                logger.warning("폴러가 폴링 주기를 받아들이지 않았습니다")
                return False
        self.preferences_store().set_poll_seconds(value)
        return True

    def _menu_quit(self) -> None:
        """메뉴 "종료": launchd 관리를 해제한 뒤 중지한다.

        ``bootout``이 없으면 launchd ``KeepAlive``가 프로세스를 즉시 다시 시작한다;
        메뉴 바 컨트롤러는 실행 루프를 멈추므로 이후에도 순서 있는 종료가 실행된다.
        CLI ``sesametok-backend stop``과 같은 경로다.
        """

        if self.managed:
            try:
                from .launcher import LaunchAgent

                result = LaunchAgent(self.settings).bootout()
                if not result.ok:
                    logger.warning("launchctl bootout에 실패했습니다: %s", result.message)
            except Exception:
                logger.exception("LaunchAgent를 해제하지 못했습니다")
        self.request_stop()

    def restart_enabled(self) -> bool:
        """launchd가 이 프로세스를 관리하고 있으면 True.

        메뉴의 "재시작" 항목은 이 값이 참일 때만 활성화되며, 아니면 이유를 제목에
        담아 비활성으로 남는다(개발용 ``foreground`` 실행은 launchd가 다시 띄우지
        않으므로 재시작할 대상이 없다).
        """

        return bool(self.managed)

    def _menu_restart(self) -> None:
        """메뉴 "재시작": launchd에 제자리 재시작을 요청한 뒤 중지한다.

        ``kickstart -k``가 실행 중인 인스턴스를 종료하고 다시 띄운다. 여기서는 곧바로
        ``request_stop``을 걸어 메뉴 바 컨트롤러가 런 루프를 멈추게 하고, 순서 있는
        종료가 끝나면 launchd가 새 프로세스를 시작한다. 관리 대상이 아니면 아무것도
        하지 않는다(메뉴에서도 비활성이다). ``cli.py``는 임포트하지 않는다.
        """

        if not self.managed:
            logger.warning("launchd로 실행 중일 때만 재시작할 수 있습니다")
            return
        try:
            from .launcher import LaunchAgent

            result = LaunchAgent(self.settings).kickstart(restart=True)
            if not result.ok:
                logger.warning("launchctl kickstart에 실패했습니다: %s", result.message)
        except Exception:
            logger.exception("LaunchAgent를 다시 시작하지 못했습니다")
        self.request_stop()

    def _wait_plain(self) -> None:
        while not self._stop.wait(0.5):
            pass

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: object) -> None:
            logger.info("시그널 %s을(를) 받았습니다; 종료합니다", signum)
            self.request_stop()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, handle)
            except ValueError:
                # 메인 스레드가 아니다; 호출자가 직접 종료를 이끈다.
                pass

    # --------------------------------------------------------------------- 헬스

    def health(self) -> dict:
        poller = self._poller
        archive_ready = self._archive_key is not None and self.account_hash is not None
        return {
            "running": self._started,
            "kakaotalk_running": self.diagnostic_report.get("kakaotalk_running"),
            # 읽기 경로가 실제로 동작하는지를 "준비됨"의 기준으로 삼는다. 전체 디스크
            # 접근 권한은 카카오톡 저장소가 보호되는 경우에만 필요하다.
            "permissions_ok": bool(self.diagnostic_report.get("store_readable")),
            "store_readable": bool(self.diagnostic_report.get("store_readable")),
            "full_disk_access": self.diagnostic_report.get("full_disk_access"),
            "accessibility_trusted": self.diagnostic_report.get("accessibility_trusted"),
            "archive_ready": archive_ready,
            "polling": bool(poller and not poller.paused and not poller.stopping),
            "last_sync_at": poller.stats.last_success_at if poller else None,
            "consecutive_failures": poller.stats.consecutive_failures if poller else 0,
        }


def _version() -> str:
    from . import __version__

    return __version__


def running_pid(settings: Settings) -> int | None:
    """이 설정에 대한 살아 있는 백엔드의 PID, 없으면 ``None``."""

    if not ProcessLock.is_held(settings.lock_path):
        return None
    return ProcessLock(settings.lock_path).owner_pid()
