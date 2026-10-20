"""``NSStatusItem`` 메뉴바 뷰 계층(PyObjC, 메인 스레드 전용).

이것은 메뉴바의 AppKit 쪽 절반이다. 모든 판단은
:mod:`sesametok_backend.app.status`에 있고, 이 모듈은 그것을 그려서 사용자 의도를
콜백으로 전달할 뿐이다.

이 모듈이 강제하는 규칙:

* **메인 스레드 전용.** 모든 AppKit 객체(``NSStatusItem``, ``NSImage``,
  ``NSMenu``, ``NSPasteboard``)는 메인 스레드에서만 만진다. ``set_status``는 어느
  스레드에서든 호출할 수 있으며, 호출자를 막지 않고 ``AppHelper.callAfter``를 통해
  메인 스레드로 전달한다.
* **Dock 아이콘 없음.** 활성화 정책은 ``Accessory``다.
* **템플릿 이미지.** SF Symbol은 템플릿으로 표시해 아이콘이 메뉴바의 밝은/어두운
  모드에 맞춰지게 하고, 심볼이 없으면 텍스트 제목으로 대체한다.
* **개인 데이터 없음.** 툴팁과 메뉴 텍스트는 상태, 개수, 타임스탬프만 담은 순수
  요약에서 온다.

AppKit 없이 이 모듈을 임포트하면 명확한 :class:`ImportError`가 발생한다.
:mod:`sesametok_backend.app.status` 임포트는 AppKit을 전혀 요구하지 않는다.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

try:  # pragma: no cover - 임포트 가드. pyobjc가 없는 곳에서만 실행된다
    import AppKit
    import Foundation
    import objc
    from PyObjCTools import AppHelper
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "sesametok_backend.app.menubar에는 AppKit(pyobjc-framework-Cocoa)이 필요합니다. "
        "프로젝트 런타임 의존성을 설치하거나, 헤드리스로 실행하려면 "
        "SESAMETOK_NO_MENUBAR=1을 설정하세요."
    ) from exc

from .. import diagnostics
from . import status as status_model
from .status import MenuItem, Status, StatusSnapshot

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

logger = logging.getLogger("sesametok_backend.app.menubar")

#: SF Symbol을 만들 수 없을 때(구형 macOS, 조회 실패) 쓰는 텍스트 제목.
FALLBACK_TITLES: dict[Status, str] = {
    Status.starting: "◌",
    Status.ok: "●",
    Status.degraded: "▲",
    Status.paused: "❚❚",
    Status.sending: "↑",
    Status.error: "✕",
}

_SELECTOR_PERFORM = b"performAction:"


# --------------------------------------------------------------------- 열기


def _spawn_open(args: list[str]) -> None:
    try:
        subprocess.run(["open", *args], check=False)
    except (OSError, subprocess.SubprocessError):
        logger.warning("%s을(를) 열 수 없습니다", args[0] if args else "?")


def open_path(path: str) -> None:
    """macOS ``open``으로 *path*를 연다(로그 파일)."""

    _spawn_open([path])


def open_url(url: str) -> None:
    """시스템 설정 URL을 연다(``diagnostics.*_URL``)."""

    _spawn_open([url])


def open_application(name: str) -> None:
    """번들 이름으로 설치된 애플리케이션을 연다."""

    _spawn_open(["-a", name])


def _autostart_unavailable() -> bool | None:
    """기본 로그인 자동시작 토글: 사용할 수 없음을 알리고 아무것도 바꾸지 않는다."""

    logger.warning("이 빌드에서는 로그인 자동시작 토글을 사용할 수 없습니다")
    return None


def _default_quit() -> None:
    """기본 종료: NSApplication을 종료한다(launchd가 다시 살릴 수 있다)."""

    AppKit.NSApplication.sharedApplication().terminate_(None)


# ------------------------------------------------------------ 상태 공급자


class RuntimeStatusProvider:
    """실제 :class:`Runtime`에서 :class:`StatusSnapshot`을 만든다.

    상태를 위해 ``Runtime.health()``와 ``poller.stats``를 읽고, 방 개수와 webhook
    목록을 위해 캐시된 아카이브 읽기를 재사용한다. 그래야 메뉴 갱신이 매 주기마다
    암호화 연결을 열지 않는다. 개인 데이터는 읽지 않는다.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        archive_interval: float = 30.0,
        autostart_provider: Callable[[], bool] | None = None,
        restart_provider: Callable[[], bool] | None = None,
        interval_provider: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._archive_interval = archive_interval
        self._autostart_provider = autostart_provider
        self._restart_provider = restart_provider
        self._interval_provider = interval_provider
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
        interval = self._interval_provider() if self._interval_provider is not None else None
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
            store_readable=bool(report.get("store_readable", False)),
            accessibility_trusted=report.get("accessibility_trusted"),
            autostart=bool(self._autostart_provider()) if self._autostart_provider else False,
            managed=bool(self._restart_provider()) if self._restart_provider else False,
            poll_seconds=interval,
            webhooks=webhooks,
        )

    def _archive_state(self) -> tuple[int | None, tuple[Mapping[str, object], ...]]:
        now = self._clock()
        if self._archive_at is not None and now - self._archive_at < self._archive_interval:
            return self._rooms, self._webhooks
        try:
            archive = self._runtime.archive_factory()()
        except Exception:
            logger.debug("메뉴 상태를 위한 아카이브가 아직 준비되지 않았습니다", exc_info=True)
            return self._rooms, self._webhooks
        try:
            self._rooms = archive.count_rooms()
            self._webhooks = tuple(archive.list_webhooks(include_secret=False))
            self._archive_at = now
        except Exception:
            logger.debug("아카이브 메뉴 상태를 읽지 못했습니다", exc_info=True)
        finally:
            archive.close()
        return self._rooms, self._webhooks


# ------------------------------------------------------------------ 컨트롤러


class MenuBarController(Foundation.NSObject):
    """``NSStatusItem``과 그 메뉴를 소유한다. 모든 변경은 메인 스레드 전용이다."""

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
        self._restart_callback: Callable[[], None] | None = None
        self._interval_callback: Callable[[float], None] | None = None
        self._executable_path = ""
        self._log_path = ""
        self._menu_open = False
        self._pending_snapshot: StatusSnapshot | None = None
        self._last_signature: tuple | None = None
        return self

    # -------------------------------------------------------------- 연결

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
        set_interval: Callable[[float], None] | None = None,
        quit: Callable[[], None] | None = None,
        restart: Callable[[], None] | None = None,
        open_kakaotalk: Callable[[], None] | None = None,
        open_permissions: Callable[[str], None] | None = None,
        executable_path: str = "",
        log_path: str = "",
    ) -> None:
        """콜백을 설치한다. 모두 선택 사항이며 안전한 기본값을 가진다.

        로그인 자동시작 콜백은 :func:`_autostart_unavailable`로 기본 설정되어,
        launcher 마일스톤이 실제 콜백을 주입하기 전까지 이 모듈이 ``launcher.py``와
        독립적으로 유지된다. *restart*는 launchd 관리 하에서만 의미가 있으며, 활성
        여부는 :attr:`StatusSnapshot.managed`가 결정한다. *set_interval*은 "동기화
        주기" 하위 메뉴에서 고른 초를 받아 즉시 적용한다.
        """

        self._status_provider = status_provider
        self._executable_path = executable_path
        self._log_path = log_path

        def _refresh_after(action: Callable[[], object] | None) -> None:
            if action is not None:
                action()
            self.refresh()

        self._quit_callback = quit or _default_quit
        self._restart_callback = restart
        self._interval_callback = set_interval
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
            status_model.ACTION_RESTART: self._restart,
        }

    @objc.python_method
    def _quit(self) -> None:
        """주입된 종료 콜백을 실행하고, 정리 작업이 돌도록 런 루프를 멈춘다."""

        try:
            self._quit_callback()
        finally:
            # 기본 콜백은 NSApp을 종료한다. 런타임 콜백은 런타임을 중지 상태로
            # 표시할 뿐이므로, 여기서 루프를 멈추는 것이 항상 옳다.
            self.stop()

    @objc.python_method
    def _restart(self) -> None:
        """주입된 재시작 콜백을 실행하고 런 루프를 멈춰 순서 있는 종료를 유도한다.

        런타임 콜백이 ``launchctl kickstart -k``로 재시작을 요청하므로 launchd가 곧
        프로세스를 다시 띄운다. 콜백이 없으면 아무것도 하지 않는다(비관리 프로세스는
        메뉴에서 항목이 비활성이라 여기까지 오지 않는다).
        """

        if self._restart_callback is None:
            logger.warning("재시작 콜백이 설정되지 않았습니다")
            return
        snapshot = self._snapshot
        if snapshot is None or not snapshot.managed:
            # 메뉴에서도 비활성이지만, 여기서 한 번 더 막아 비관리 프로세스가
            # 실수로 런 루프를 멈추지 않게 한다.
            logger.warning("launchd로 실행 중일 때만 재시작할 수 있습니다")
            return
        try:
            self._restart_callback()
        finally:
            self.stop()

    # --------------------------------------------------------------- 설치

    @objc.python_method
    def install(self) -> None:
        """상태 아이템과 메뉴를 만든다. 메인 스레드에서 한 번만 호출한다."""

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._menu = AppKit.NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._menu.setDelegate_(self)
        self._status_item.setMenu_(self._menu)
        self._installed = True

        if self._status_provider is not None:
            self.set_status(self._status_provider())

    @objc.python_method
    def start_refresh(self, interval: float = 1.0) -> None:
        """*interval*초마다 상태를 다시 읽는 데몬 스레드를 시작한다."""

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
                logger.exception("메뉴바 상태 갱신에 실패했습니다")

    # ---------------------------------------------------------------- 상태

    @objc.python_method
    def set_status(self, snapshot: StatusSnapshot) -> None:
        """*snapshot*을 그린다. 메인 스레드로 전달하며 결코 블로킹하지 않는다."""

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
        if self._menu_open:
            # 열려 있는 메뉴를 다시 그리면 펼쳐 둔 하위 메뉴가 접히고 다시 나타난다.
            # 닫힌 뒤에 한 번만 반영한다.
            self._pending_snapshot = snapshot
            return
        signature = status_model.menu_signature(
            snapshot, executable_path=self._executable_path
        )
        if signature == self._last_signature:
            return
        self._last_signature = signature
        self._rebuild_menu(snapshot)

    def menuWillOpen_(self, menu: Any) -> None:
        """메뉴가 열린 동안에는 다시 그리지 않는다(하위 메뉴가 접히는 것을 막는다)."""

        self._menu_open = True

    def menuDidClose_(self, menu: Any) -> None:
        self._menu_open = False
        # 닫히는 도중에 항목을 갈아 끼우지 않도록 다음 런루프로 미룬다.
        AppHelper.callAfter(self._flush_pending, None)

    @objc.python_method
    def _flush_pending(self, *_args: Any) -> None:
        pending, self._pending_snapshot = self._pending_snapshot, None
        snapshot = pending or self._snapshot
        if snapshot is None:
            return
        signature = status_model.menu_signature(
            snapshot, executable_path=self._executable_path
        )
        if signature == self._last_signature and pending is None:
            return
        self._last_signature = signature
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
        # 툴팁은 첫 메뉴 줄과 같은 텍스트 요약을 보여준다.
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
        if item.kind in (status_model.KIND_CHECKBOX, status_model.KIND_INTERVAL):
            nsitem.setState_(
                AppKit.NSControlStateValueOn
                if item.checked
                else AppKit.NSControlStateValueOff
            )
        if item.value is not None:
            # 폴링 주기 선택지의 초 값은 태그로 전달한다(문자열 id는 action id 그대로).
            nsitem.setTag_(int(round(item.value)))
        if item.action is not None:
            nsitem.setTarget_(self)
            nsitem.setAction_(_SELECTOR_PERFORM)
            nsitem.setRepresentedObject_(item.action)
            if item.action == status_model.ACTION_QUIT:
                nsitem.setKeyEquivalent_("q")
                nsitem.setKeyEquivalentModifierMask_(AppKit.NSEventModifierFlagCommand)
        if item.children:
            submenu = AppKit.NSMenu.alloc().init()
            submenu.setAutoenablesItems_(False)
            for child in item.children:
                submenu.addItem_(self._nsitem(child))
            nsitem.setSubmenu_(submenu)
        return nsitem

    def performAction_(self, sender: Any) -> None:
        """단일 AppKit 액션. 항목이 표현하는 action id로 분기한다."""

        action = sender.representedObject()
        if action == status_model.ACTION_SET_INTERVAL:
            # 주기 선택은 "어떤 값"이 필요하므로 태그를 읽는 별도 경로를 쓴다.
            self._set_interval_action(sender)
            return
        handler = self._actions.get(action)
        if handler is None:
            logger.warning("메뉴 액션 %r에 대한 핸들러가 없습니다", action)
            return
        try:
            handler()
        except Exception:
            logger.exception("메뉴 액션 %r이(가) 실패했습니다", action)

    @objc.python_method
    def _set_interval_action(self, sender: Any) -> None:
        """하위 메뉴에서 고른 폴링 주기를 즉시 적용한다.

        AppKit 액션이므로 메인 스레드에서 호출된다. 콜백은 폴러를 깨우고 저장소에
        기록하며, 저장 실패는 콜백 안에서 로그만 남긴다. 적용 뒤 메뉴를 다시 그려
        체크 표시를 옮긴다.
        """

        try:
            seconds = float(sender.tag())
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds <= 0:
            logger.warning("동기화 주기 값이 비정상이라 무시했습니다")
            return
        if self._interval_callback is None:
            logger.warning("동기화 주기를 바꿀 콜백이 없습니다")
            return
        try:
            self._interval_callback(seconds)
        except Exception:
            logger.exception("동기화 주기를 바꾸지 못했습니다")
        finally:
            self.refresh()

    @objc.python_method
    def _copy_token(self, supplier: Callable[[], str | None] | None) -> None:
        token = supplier() if supplier is not None else None
        if not token:
            logger.warning("복사할 API 토큰이 없습니다")
            return
        pasteboard = AppKit.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(token, AppKit.NSPasteboardTypeString)

    @objc.python_method
    def _open_log(self) -> None:
        if not self._log_path:
            logger.warning("로그 경로가 설정되지 않았습니다")
            return
        open_path(self._log_path)

    # ----------------------------------------------------------------- 종료

    def stop(self) -> None:
        """종료 훅: 갱신 스레드와 AppKit 이벤트 루프를 멈춘다."""

        if self._stop_event.is_set():
            return
        self._stop_event.set()
        if Foundation.NSThread.isMainThread():
            self._stop_loop()
        else:
            AppHelper.callAfter(self._stop_loop)

    @objc.python_method
    def _stop_loop(self) -> None:
        """``NSApp.run()``을 멈춰서 이후에 순서 있는 종료가 그대로 실행되게 한다.

        ``AppHelper.stopEventLoop()``는 ``NSApp.terminate_()``를 호출하는데, 이는
        ``Runtime.shutdown()``이 끝나기 전에 프로세스를 종료해 버린다. 순수한
        ``stop_``은 런 루프만 끝내며, wake 이벤트를 보내면 즉시 효력이 난다.
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


# ------------------------------------------------------------------ 진입점


def _trigger_sync(runtime: Runtime) -> None:
    poller = runtime.poller
    if poller is None:
        logger.warning("동기화할 폴러가 없습니다")
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
    """앱 루프를 실행해 멈출 때 ``runEventLoop``이 반환되게 한다.

    ``AppHelper.runEventLoop``는 기본적으로 ``NSApplicationMain``을 쓰는데, 이는
    결코 반환하지 않고 앱이 종료되는 즉시 프로세스를 끝내 버린다. 그러면 순서 있는
    종료가 건너뛰어진다. ``NSApp.run()``을 우리가 직접 실행하면 프로세스가 살아
    있으므로 메뉴바가 멈춘 뒤에도 ``Runtime.shutdown()``이 여전히 실행된다.
    """

    AppKit.NSApplication.sharedApplication().run()


def run_menubar(
    runtime: Runtime,
    *,
    refresh_seconds: float = 1.0,
    archive_interval: float = 30.0,
    quit_callback: Callable[[], None] | None = None,
    restart_callback: Callable[[], None] | None = None,
    autostart_provider: Callable[[], bool] | None = None,
    toggle_autostart: Callable[[], bool | None] | None = None,
    restart_provider: Callable[[], bool] | None = None,
    interval_provider: Callable[[], float] | None = None,
    set_interval: Callable[[float], None] | None = None,
) -> int:
    """*runtime*에서 메뉴바를 만들고 AppKit 이벤트 루프를 실행한다.

    루프를 멈추는 종료 훅을 등록하므로 어느 스레드에서든 ``runtime.shutdown()``이
    동작한다. *quit_callback*은 "백엔드 종료"를 처리하며, 기본값은
    ``NSApplication``을 종료하는 것이다. *restart_callback*은 launchd 관리 하에서
    프로세스를 재시작한다. *autostart_provider*는 로그인 자동시작 체크박스를 채우고,
    *toggle_autostart*는 그 값을 바꾼다. *restart_provider*는 재시작 항목의 활성
    여부(launchd 관리 여부)를 알려 주어, 비활성일 때 메뉴가 이유를 표시할 수 있게
    한다. *interval_provider*는 "동기화 주기" 하위 메뉴의 체크 표시를 정하고,
    *set_interval*은 고른 초를 즉시 적용한다. 메인 스레드에서 호출해야 한다.
    """

    provider = RuntimeStatusProvider(
        runtime,
        archive_interval=archive_interval,
        autostart_provider=autostart_provider,
        restart_provider=restart_provider,
        interval_provider=interval_provider,
    )
    controller = MenuBarController.alloc().init()
    controller.configure(
        status_provider=provider.snapshot,
        sync_now=lambda: _trigger_sync(runtime),
        toggle_pause=lambda: _toggle_pause(runtime),
        toggle_autostart=toggle_autostart,
        set_interval=set_interval,
        copy_api_token=lambda: runtime.api_token,
        open_log=lambda: open_path(str(runtime.settings.backend_log_path)),
        quit=quit_callback,
        restart=restart_callback,
        executable_path=diagnostics.python_executable(),
        log_path=str(runtime.settings.backend_log_path),
    )
    controller.install()
    runtime.add_shutdown_hook(controller.stop)
    controller.start_refresh(refresh_seconds)
    logger.info("메뉴바를 시작했습니다")
    try:
        AppHelper.runEventLoop(main=_run_app)
    finally:
        controller.stop()
    logger.info("메뉴바를 멈췄습니다")
    return 0
