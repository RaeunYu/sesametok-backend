"""The single backend process.

``Runtime`` owns process-level concerns and assembles the pipeline:

    identity + key  →  encrypted archive  →  poller (thread B)  →  event bus
                                             ↓
                                  webhooks, send worker, API

Shutdown is ordered and idempotent: hooks run in reverse registration order, so
the API stops first, then the send worker drains, then webhooks, and the poller
stops last. Every service except the poller is skipped when ``settings.api_enabled``
is false (``SESAMETOK_API=0``), which is how tests and headless CI run.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import diagnostics
from .config import Settings
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

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing the API eagerly
    from .api.server import ApiService
    from .api.webhooks import WebhookDispatcher
    from .core.send_queue import SendQueue

logger = logging.getLogger("sesametok_backend.runtime")


class AlreadyRunningError(RuntimeError):
    """Raised when another live backend process holds the instance lock."""


class StartupError(RuntimeError):
    """Raised when the backend cannot start (identity, key, or archive)."""


@dataclass
class Runtime:
    settings: Settings
    foreground: bool = True
    managed: bool = False
    """True when launched by launchd, where stderr becomes a log file."""

    interactive: bool | None = None
    api_token: str | None = None
    diagnostic_report: dict = field(default_factory=dict)
    identity: AccountIdentity | None = None
    account_hash: str | None = None
    archive_key_source: str | None = None
    # Injectable seams: production builds the real KakaoTalk source and resolvers;
    # tests substitute quiet ones so no test touches the real installation.
    source: PollSource | None = None
    identity_resolver: Callable[[Settings], AccountIdentity] | None = None
    archive_key_resolver: Callable[[Settings], ArchiveKey] | None = None
    _lock: ProcessLock = field(init=False)
    _stop: threading.Event = field(init=False, default_factory=threading.Event)
    _started: bool = field(init=False, default=False)
    _shutdown_hooks: list[Callable[[], None]] = field(init=False, default_factory=list)
    # Serializes concurrent shutdown() calls: the menu bar stops the run loop from
    # a background thread while run_foreground's finally may also call shutdown().
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

    # --------------------------------------------------------------- lifecycle

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
        """True while one send is in flight (drives ``Status.sending``)."""

        return bool(self._send_queue is not None and self._send_queue.stats.in_flight)

    def archive_factory(self) -> Callable[[], Archive]:
        """A factory for per-thread read connections (API workers)."""

        def factory() -> Archive:
            if self._archive_key is None or self.account_hash is None:
                raise StartupError("the archive is not ready")
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
            raise AlreadyRunningError(f"another sesametok-backend is already running{detail}")

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
        logger.info("sesametok-backend %s starting", _version())
        logger.info("config %s", self.settings.redacted())
        logger.info("account resolved via %s; archive key via %s",
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
                f"{exc}\nSet SESAMETOK_ARCHIVE_KEY, or allow access to the login "
                "keychain, then retry."
            ) from exc

        self._archive_key = resolved.key
        self.archive_key_source = resolved.source
        if resolved.is_new:
            self._announce_new_archive_key(resolved)

    def _announce_new_archive_key(self, resolved: ArchiveKey) -> None:
        message = [
            "A new archive key was generated and stored in the login keychain.",
            KEY_BACKUP_HELP,
        ]
        for line in message:
            logger.warning("%s", line)
        # Never print the key when stderr is a launchd log file.
        if self.interactive:
            print(
                "\n  archive key: "
                + key_to_hex(resolved.key)
                + "\n  Save it now; it is shown here only because this is an "
                "interactive first run.\n",
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
                "could not determine the KakaoTalk account: "
                f"{exc}\nSet SESAMETOK_USER_ID to the account's numeric user id "
                "and retry."
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
                "archive ready: %d rooms, %d messages, %d events",
                archive.count_rooms(),
                archive.count_messages(),
                archive.latest_seq(),
            )
        finally:
            archive.close()

    def _start_poller(self) -> None:
        assert self.identity is not None
        source = self.source or KakaoSource(identity=self.identity, roots=store.store_roots())
        self._poller = Poller(self.settings, self.archive_factory(), self._bus, source)
        self._poller.start()
        self.add_shutdown_hook(self._stop_poller)

    def _stop_poller(self) -> None:
        if self._poller is None:
            return
        self._poller.stop()
        self._poller.join(timeout=10)
        self._poller = None

    # ------------------------------------------------------ webhooks / sending

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
        """Build the send worker, but never let a missing UI driver block startup.

        ``SendQueue`` starts lazily on its first ``submit()``; the Accessibility
        driver is constructed eagerly only to fail fast if the frameworks are
        unavailable, and a failure just disables sending.
        """

        from .core.send_queue import SendQueue

        try:
            from .send.accessibility import KakaoTalkDriver
            from .send.curtain import Curtain

            sender = KakaoTalkDriver(curtain=Curtain())
        except Exception as exc:
            logger.warning("sending disabled: %s", type(exc).__name__)
            return
        self._send_queue = SendQueue(self.settings, self.archive_factory(), self._bus, sender)
        self.add_shutdown_hook(self._stop_send_worker)

    def _stop_send_worker(self) -> None:
        queue = self._send_queue
        if queue is None:
            return
        queue.stop()
        # Never joined before it started: SendQueue starts on its first submit().
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
        """Extra ``GET /v1/status`` fields, redacted (no ids, keys, or tokens)."""

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
        """Subscription counts for ``/v1/status``; no URLs or secrets."""

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
        """Stop every service once, in reverse registration order.

        Idempotent and safe to call from any thread; a second concurrent caller
        waits for the first teardown to finish and then does nothing.
        """

        with self._shutdown_lock:
            self._stop.set()
            hooks = list(reversed(self._shutdown_hooks))
            self._shutdown_hooks.clear()
            for hook in hooks:
                try:
                    hook()
                except Exception:
                    logger.exception("shutdown hook failed")
            if self._started:
                logger.info("sesametok-backend stopped")
            self._lock.release()
            self._started = False

    # ---------------------------------------------------------------- runners

    def run_foreground(self, install_signal_handlers: bool = True) -> int:
        # Install handlers first: a slow first-run identity lookup (the reverse
        # search can take ~60s) must still be interruptible, and a signal during
        # startup must not leave a half-initialized archive behind.
        if install_signal_handlers:
            self._install_signal_handlers()
        self.start()
        try:
            if self._menubar_enabled():
                self._run_menubar()
            else:
                self._wait_plain()
        except KeyboardInterrupt:
            logger.info("interrupted")
        finally:
            self.shutdown()
        return 0

    def _menubar_enabled(self) -> bool:
        """True when this process should own the AppKit menu bar.

        AppKit is main-thread only, and tests/CI run
        headless via ``SESAMETOK_NO_MENUBAR=1``.
        """

        return (
            not self.settings.no_menubar
            and threading.current_thread() is threading.main_thread()
        )

    def _run_menubar(self) -> None:
        """Run the PyObjC menu bar run loop (a no-op wait when AppKit is absent)."""

        try:
            from .app.menubar import run_menubar
        except ImportError as exc:
            logger.error("menu bar unavailable (%s); running headless", exc)
            self._wait_plain()
            return
        run_menubar(
            self,
            quit_callback=self._menu_quit,
            autostart_provider=self.autostart_enabled,
            toggle_autostart=self.toggle_autostart,
        )

    def autostart_enabled(self) -> bool:
        """True when a LaunchAgent is installed for this user.

        The menu checkbox reflects the plist's presence, not whether the loaded
        job happens to be running right now.
        """

        try:
            from .launcher import LaunchAgent, plist_path_is_allowed

            agent = LaunchAgent(self.settings)
            if not plist_path_is_allowed(agent.plist_path):
                return False
            return agent.plist_path.exists()
        except Exception:
            logger.debug("could not read the login-autostart state", exc_info=True)
            return False

    def toggle_autostart(self) -> bool | None:
        """Install or remove the LaunchAgent. Returns the new state, or ``None``.

        Disabling removes the plist only: the running process is left alone (it
        was started explicitly), and it simply will not come back at next login.
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
            logger.warning("login autostart is not available for this plist path")
            return None
        except Exception:
            logger.debug("login autostart is unavailable", exc_info=True)
            return None

        try:
            if agent.plist_path.exists():
                agent.remove_plist()
                logger.info(
                    "login autostart disabled; this process keeps running until logout"
                )
                return False

            agent.install_plist()
            result = agent.bootstrap()
            if not result.ok:
                logger.warning("launchctl bootstrap failed: %s", result.message)
                agent.remove_plist()
                return None
            logger.info("login autostart enabled")
            return True
        except Exception as exc:
            logger.warning("login autostart toggle failed: %s", type(exc).__name__)
            return None

    def _menu_quit(self) -> None:
        """Menu "백엔드 종료": unregister launchd management, then stop.

        Without ``bootout`` a launchd ``KeepAlive`` would immediately restart the
        process; the menu-bar controller stops the run loop so ordered shutdown
        still runs afterwards.
        """

        if self.managed:
            try:
                from .launcher import LaunchAgent

                result = LaunchAgent(self.settings).bootout()
                if not result.ok:
                    logger.warning("launchctl bootout failed: %s", result.message)
            except Exception:
                logger.exception("could not unregister the LaunchAgent")
        self.request_stop()

    def _wait_plain(self) -> None:
        while not self._stop.wait(0.5):
            pass

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: object) -> None:
            logger.info("received signal %s; shutting down", signum)
            self.request_stop()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, handle)
            except ValueError:
                # Not on the main thread; the caller drives shutdown itself.
                pass

    # ----------------------------------------------------------------- health

    def health(self) -> dict:
        poller = self._poller
        archive_ready = self._archive_key is not None and self.account_hash is not None
        return {
            "running": self._started,
            "kakaotalk_running": self.diagnostic_report.get("kakaotalk_running"),
            "permissions_ok": bool(
                self.diagnostic_report.get("full_disk_access_ok")
                and self.diagnostic_report.get("accessibility_trusted") is not False
            ),
            "archive_ready": archive_ready,
            "polling": bool(poller and not poller.paused and not poller.stopping),
            "last_sync_at": poller.stats.last_success_at if poller else None,
            "consecutive_failures": poller.stats.consecutive_failures if poller else 0,
        }


def _version() -> str:
    from . import __version__

    return __version__


def running_pid(settings: Settings) -> int | None:
    """PID of a live backend for these settings, or ``None``."""

    if not ProcessLock.is_held(settings.lock_path):
        return None
    return ProcessLock(settings.lock_path).owner_pid()
