"""``NSStatusItem`` menu bar view layer (PyObjC, main thread only).

This is the AppKit half of the menu bar. All decisions live in
:mod:`sesametok_backend.app.status`; this module only renders them and forwards
user intent to callbacks.

Rules this module enforces:

* **Main thread only.** Every AppKit object (``NSStatusItem``, ``NSImage``,
  ``NSMenu``, ``NSPasteboard``) is touched on the main thread. ``set_status`` may
  be called from any thread and marshals through ``AppHelper.callAfter`` without
  blocking the caller.
* **No Dock icon.** The activation policy is ``Accessory``.
* **Template image.** The SF Symbol is marked as a template so the icon adapts to
  the light/dark menu bar, with a text title fallback when the symbol is missing.
* **No personal data.** Tooltips and menu text come from the pure summary, which
  contains state, counts, and timestamps only.

Importing this module without AppKit raises a clear :class:`ImportError`; importing
:mod:`sesametok_backend.app.status` never requires AppKit.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

try:  # pragma: no cover - import guard, exercised only where pyobjc is absent
    import AppKit
    import Foundation
    import objc
    from PyObjCTools import AppHelper
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "sesametok_backend.app.menubar requires AppKit (pyobjc-framework-Cocoa). "
        "Install the project runtime dependencies, or set SESAMETOK_NO_MENUBAR=1 "
        "to run headless."
    ) from exc

from .. import diagnostics
from . import status as status_model
from .status import MenuItem, Status, StatusSnapshot

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

logger = logging.getLogger("sesametok_backend.app.menubar")

#: Text titles used when an SF Symbol cannot be built (older macOS, lookup miss).
FALLBACK_TITLES: dict[Status, str] = {
    Status.starting: "◌",
    Status.ok: "●",
    Status.degraded: "▲",
    Status.paused: "❚❚",
    Status.sending: "↑",
    Status.error: "✕",
}

_SELECTOR_PERFORM = b"performAction:"


# --------------------------------------------------------------------- openers


def _spawn_open(args: list[str]) -> None:
    try:
        subprocess.run(["open", *args], check=False)
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not open %s", args[0] if args else "?")


def open_path(path: str) -> None:
    """Open *path* with macOS ``open`` (log file)."""

    _spawn_open([path])


def open_url(url: str) -> None:
    """Open a System Settings URL (``diagnostics.*_URL``)."""

    _spawn_open([url])


def open_application(name: str) -> None:
    """Open an installed application by bundle name."""

    _spawn_open(["-a", name])


def _autostart_unavailable() -> bool | None:
    """Default login-autostart toggle: report unavailability, change nothing."""

    logger.warning("login autostart toggle is not available in this build")
    return None


def _default_quit() -> None:
    """Default quit: terminate the NSApplication (launchd may restart it)."""

    AppKit.NSApplication.sharedApplication().terminate_(None)


# ------------------------------------------------------------ status provider


class RuntimeStatusProvider:
    """Builds :class:`StatusSnapshot` from a live :class:`Runtime`.

    Reads ``Runtime.health()`` and ``poller.stats`` for state and reuses a cached
    archive read for the room count and webhook list, so the menu refresh never
    opens an encrypted connection every tick. No personal data is read.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        archive_interval: float = 30.0,
        autostart_provider: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._archive_interval = archive_interval
        self._autostart_provider = autostart_provider
        self._clock = clock
        self._rooms: int | None = None
        self._webhooks: tuple[Mapping[str, object], ...] = ()
        self._archive_at: float | None = None

    def snapshot(self) -> StatusSnapshot:
        health = self._runtime.health()
        poller = self._runtime.poller
        stats = poller.stats if poller is not None else None
        rooms, webhooks = self._archive_state()
        report = self._runtime.diagnostic_report
        return status_model.build_snapshot(
            kakaotalk_running=health.get("kakaotalk_running"),
            permissions_ok=bool(health.get("permissions_ok")),
            polling=bool(health.get("polling")),
            paused=bool(poller is not None and poller.paused),
            sending=bool(getattr(self._runtime, "sending", False)),
            consecutive_failures=(
                stats.consecutive_failures
                if stats is not None
                else int(health.get("consecutive_failures") or 0)
            ),
            last_sync_at=stats.last_success_at if stats is not None else health.get("last_sync_at"),
            starting=not bool(health.get("running")),
            room_count=rooms,
            full_disk_access_ok=bool(report.get("full_disk_access_ok", True)),
            accessibility_trusted=report.get("accessibility_trusted"),
            autostart=bool(self._autostart_provider()) if self._autostart_provider else False,
            webhooks=webhooks,
        )

    def _archive_state(self) -> tuple[int | None, tuple[Mapping[str, object], ...]]:
        now = self._clock()
        if self._archive_at is not None and now - self._archive_at < self._archive_interval:
            return self._rooms, self._webhooks
        try:
            archive = self._runtime.archive_factory()()
        except Exception:
            logger.debug("archive not ready for menu status", exc_info=True)
            return self._rooms, self._webhooks
        try:
            self._rooms = archive.count_rooms()
            self._webhooks = tuple(archive.list_webhooks(include_secret=False))
            self._archive_at = now
        except Exception:
            logger.debug("could not read archive menu status", exc_info=True)
        finally:
            archive.close()
        return self._rooms, self._webhooks


# ------------------------------------------------------------------ controller


class MenuBarController(Foundation.NSObject):
    """Owns the ``NSStatusItem`` and its menu; every mutation is main-thread only."""

    def init(self) -> Any:
        self = objc.super(MenuBarController, self).init()
        if self is None:
            return None
        self._status_item: Any = None
        self._menu: Any = None
        self._installed = False
        self._snapshot: StatusSnapshot | None = None
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._status_provider: Callable[[], StatusSnapshot] | None = None
        self._actions: dict[str, Callable[[], None]] = {}
        self._quit_callback: Callable[[], None] = _default_quit
        self._executable_path = ""
        self._log_path = ""
        return self

    # -------------------------------------------------------------- wiring

    @objc.python_method
    def configure(
        self,
        *,
        status_provider: Callable[[], StatusSnapshot],
        sync_now: Callable[[], None] | None = None,
        toggle_pause: Callable[[], None] | None = None,
        copy_api_token: Callable[[], str | None] | None = None,
        open_log: Callable[[], None] | None = None,
        toggle_autostart: Callable[[], bool | None] | None = None,
        quit: Callable[[], None] | None = None,
        open_kakaotalk: Callable[[], None] | None = None,
        open_permissions: Callable[[str], None] | None = None,
        executable_path: str = "",
        log_path: str = "",
    ) -> None:
        """Install callbacks. Every one is optional with a safe default.

        The login-autostart callback defaults to
        :func:`_autostart_unavailable`, so this module stays independent of
        ``launcher.py`` until the launcher milestone injects a real one.
        """

        self._status_provider = status_provider
        self._executable_path = executable_path
        self._log_path = log_path

        def _refresh_after(action: Callable[[], object] | None) -> None:
            if action is not None:
                action()
            self.refresh()

        self._quit_callback = quit or _default_quit
        self._actions = {
            status_model.ACTION_SYNC_NOW: lambda: _refresh_after(sync_now),
            status_model.ACTION_TOGGLE_PAUSE: lambda: _refresh_after(toggle_pause),
            status_model.ACTION_TOGGLE_AUTOSTART: lambda: _refresh_after(
                toggle_autostart or _autostart_unavailable
            ),
            status_model.ACTION_COPY_API_TOKEN: lambda: self._copy_token(copy_api_token),
            status_model.ACTION_OPEN_LOG: lambda: (open_log or self._open_log)(),
            status_model.ACTION_OPEN_KAKAOTALK: lambda: (
                open_kakaotalk or (lambda: open_application(diagnostics.KAKAOTALK_BUNDLE))
            )(),
            status_model.ACTION_OPEN_FULL_DISK_ACCESS: lambda: (
                open_permissions or open_url
            )(diagnostics.FULL_DISK_ACCESS_URL),
            status_model.ACTION_OPEN_ACCESSIBILITY: lambda: (open_permissions or open_url)(
                diagnostics.ACCESSIBILITY_URL
            ),
            status_model.ACTION_QUIT: self._quit,
        }

    @objc.python_method
    def _quit(self) -> None:
        """Run the injected quit callback, then stop the run loop so teardown runs."""

        try:
            self._quit_callback()
        finally:
            # The default callback terminates NSApp; a runtime callback only marks
            # the runtime stopping, so stopping the loop is always correct here.
            self.stop()

    # --------------------------------------------------------------- install

    @objc.python_method
    def install(self) -> None:
        """Create the status item and menu; call once, on the main thread."""

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._menu = AppKit.NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._status_item.setMenu_(self._menu)
        self._installed = True

        if self._status_provider is not None:
            self.set_status(self._status_provider())

    @objc.python_method
    def start_refresh(self, interval: float = 1.0) -> None:
        """Start a daemon thread that re-reads the status every *interval* seconds."""

        if self._refresh_thread is not None:
            return
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            args=(interval,),
            name="sesametok-menubar-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    @objc.python_method
    def _refresh_loop(self, interval: float) -> None:
        while not self._stop_event.wait(interval):
            provider = self._status_provider
            if provider is None:
                continue
            try:
                self.set_status(provider())
            except Exception:
                logger.exception("menu bar status refresh failed")

    # ---------------------------------------------------------------- status

    @objc.python_method
    def set_status(self, snapshot: StatusSnapshot) -> None:
        """Render *snapshot*; marshals to the main thread and never blocks."""

        if not self._installed:
            self._snapshot = snapshot
            return
        if Foundation.NSThread.isMainThread():
            self._apply(snapshot)
        else:
            AppHelper.callAfter(self._apply, snapshot)

    @objc.python_method
    def refresh(self) -> None:
        provider = self._status_provider
        if provider is not None:
            self.set_status(provider())

    @objc.python_method
    def _apply(self, snapshot: StatusSnapshot) -> None:
        self._snapshot = snapshot
        self._update_button(snapshot)
        self._rebuild_menu(snapshot)

    @objc.python_method
    def _update_button(self, snapshot: StatusSnapshot) -> None:
        if self._status_item is None:
            return
        symbol = status_model.symbol_name(snapshot.status)
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol, snapshot.summary
        )
        button = self._status_item.button()
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
            button.setTitle_("")
        else:
            button.setImage_(None)
            button.setTitle_(FALLBACK_TITLES.get(snapshot.status, "?"))
        # The tooltip shows the same text summary as the first menu line.
        button.setToolTip_(snapshot.summary)

    @objc.python_method
    def _rebuild_menu(self, snapshot: StatusSnapshot) -> None:
        if self._menu is None:
            return
        self._menu.removeAllItems()
        for item in status_model.menu_items(
            snapshot, executable_path=self._executable_path
        ):
            self._menu.addItem_(self._nsitem(item))

    @objc.python_method
    def _nsitem(self, item: MenuItem) -> Any:
        if item.kind == status_model.KIND_SEPARATOR:
            return AppKit.NSMenuItem.separatorItem()
        nsitem = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            item.title, None, ""
        )
        nsitem.setEnabled_(item.enabled)
        if item.kind == status_model.KIND_CHECKBOX:
            nsitem.setState_(
                AppKit.NSControlStateValueOn
                if item.checked
                else AppKit.NSControlStateValueOff
            )
        if item.action is not None:
            nsitem.setTarget_(self)
            nsitem.setAction_(_SELECTOR_PERFORM)
            nsitem.setRepresentedObject_(item.action)
            if item.action == status_model.ACTION_QUIT:
                nsitem.setKeyEquivalent_("q")
                nsitem.setKeyEquivalentModifierMask_(AppKit.NSEventModifierFlagCommand)
        return nsitem

    def performAction_(self, sender: Any) -> None:
        """Single AppKit action; dispatches by the item's represented action id."""

        action = sender.representedObject()
        handler = self._actions.get(action)
        if handler is None:
            logger.warning("no handler for menu action %r", action)
            return
        try:
            handler()
        except Exception:
            logger.exception("menu action %r failed", action)

    @objc.python_method
    def _copy_token(self, supplier: Callable[[], str | None] | None) -> None:
        token = supplier() if supplier is not None else None
        if not token:
            logger.warning("no API token available to copy")
            return
        pasteboard = AppKit.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(token, AppKit.NSPasteboardTypeString)

    @objc.python_method
    def _open_log(self) -> None:
        if not self._log_path:
            logger.warning("no log path configured")
            return
        open_path(self._log_path)

    # ----------------------------------------------------------------- stop

    def stop(self) -> None:
        """Shutdown hook: stop the refresh thread and the AppKit event loop."""

        if self._stop_event.is_set():
            return
        self._stop_event.set()
        if Foundation.NSThread.isMainThread():
            self._stop_loop()
        else:
            AppHelper.callAfter(self._stop_loop)

    @objc.python_method
    def _stop_loop(self) -> None:
        """Stop ``NSApp.run()`` so ordered shutdown still runs afterwards.

        ``AppHelper.stopEventLoop()`` calls ``NSApp.terminate_()``, which exits the
        process before ``Runtime.shutdown()`` finishes; plain ``stop_`` only ends
        the run loop, and a wake event makes it take effect immediately.
        """

        app = AppKit.NSApplication.sharedApplication()
        app.stop_(None)
        wake = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeApplicationDefined,
            AppKit.NSMakePoint(0.0, 0.0),
            0,
            0.0,
            0,
            None,
            0,
            0,
            0,
        )
        app.postEvent_atStart_(wake, True)


# ----------------------------------------------------------------- entry point


def _trigger_sync(runtime: Runtime) -> None:
    poller = runtime.poller
    if poller is None:
        logger.warning("no poller to sync")
        return
    poller.trigger()


def _toggle_pause(runtime: Runtime) -> None:
    poller = runtime.poller
    if poller is None:
        return
    if poller.paused:
        poller.resume()
    else:
        poller.pause()


def _run_app(_argv: list[str] | None = None) -> None:
    """Run the application loop so ``runEventLoop`` returns when it stops.

    ``AppHelper.runEventLoop`` defaults to ``NSApplicationMain``, which never
    returns and exits the process as soon as the app terminates; ordered shutdown
    would then be skipped. Running ``NSApp.run()`` ourselves keeps the process
    alive so ``Runtime.shutdown()`` still executes after the menu bar stops.
    """

    AppKit.NSApplication.sharedApplication().run()


def run_menubar(
    runtime: Runtime,
    *,
    refresh_seconds: float = 1.0,
    archive_interval: float = 30.0,
    quit_callback: Callable[[], None] | None = None,
    autostart_provider: Callable[[], bool] | None = None,
    toggle_autostart: Callable[[], bool | None] | None = None,
) -> int:
    """Build the menu bar from *runtime* and run the AppKit event loop.

    Registers a shutdown hook that stops the loop, so ``runtime.shutdown()`` works
    from any thread. *quit_callback* handles "백엔드 종료"; it defaults to
    terminating the ``NSApplication``. *autostart_provider* feeds the login-autostart
    checkbox and *toggle_autostart* changes it. Must be called on the main
    thread.
    """

    provider = RuntimeStatusProvider(
        runtime, archive_interval=archive_interval, autostart_provider=autostart_provider
    )
    controller = MenuBarController.alloc().init()
    controller.configure(
        status_provider=provider.snapshot,
        sync_now=lambda: _trigger_sync(runtime),
        toggle_pause=lambda: _toggle_pause(runtime),
        toggle_autostart=toggle_autostart,
        copy_api_token=lambda: runtime.api_token,
        open_log=lambda: open_path(str(runtime.settings.backend_log_path)),
        quit=quit_callback,
        executable_path=diagnostics.python_executable(),
        log_path=str(runtime.settings.backend_log_path),
    )
    controller.install()
    runtime.add_shutdown_hook(controller.stop)
    controller.start_refresh(refresh_seconds)
    logger.info("menu bar started")
    try:
        AppHelper.runEventLoop(main=_run_app)
    finally:
        controller.stop()
    logger.info("menu bar stopped")
    return 0
