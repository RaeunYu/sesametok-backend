"""``sesametok-backend`` 커맨드라인 인터페이스.

``foreground``는 이 터미널에서 프로세스를 실행하고; ``start``/``stop``/``restart``/
``install``/``uninstall``은 launchd LaunchAgent를 관리하므로 백엔드는 터미널을 닫고
다시 로그인해도 살아남는다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unicodedata

from . import __version__, diagnostics
from .config import ConfigError, Settings
from .kakao import store
from .launcher import (
    LaunchAgent,
    LaunchdStatus,
    PlistPathNotAllowed,
    ProcessLock,
    looks_not_found,
    plist_path_is_allowed,
    require_plist_path_allowed,
    terminate_process,
    wait_until_stopped,
    wait_until_running,
)
from .logging_setup import setup_logging
from .runtime import AlreadyRunningError, Runtime, StartupError, running_pid
from .security.archive_key import (
    KEY_BACKUP_HELP,
    ArchiveKeyError,
    key_to_hex,
    resolve_archive_key,
)
from .security.tokens import load_or_create_token, token_source

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_UNAVAILABLE = 3

#: launchd가 수명주기 변경을 보고할 때까지 기다리는 시간.
START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0


def _launch_agent(settings: Settings) -> LaunchAgent:
    """*settings*에 대한 LaunchAgent를 만든다 (테스트용 이음매)."""

    return LaunchAgent(settings)


def _require_managed_plist(agent: LaunchAgent) -> bool:
    """머신이 소유한 plist 경로에 대해서는 쓰기와 ``launchctl`` 실행을 거부한다."""

    try:
        require_plist_path_allowed(agent.plist_path)
    except PlistPathNotAllowed as exc:
        print(f"sesametok-backend: {exc}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- 명령


def cmd_foreground(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    setup_logging(settings.log_level, settings.backend_log_path, foreground=True)
    runtime = Runtime(settings, foreground=True, managed=getattr(args, "managed", False))
    try:
        return runtime.run_foreground()
    except AlreadyRunningError as exc:
        print(f"sesametok-backend: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except StartupError as exc:
        print(f"sesametok-backend: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_OK


#: ``status`` 라벨 정렬 폭(터미널 표시 폭 기준).
LABEL_WIDTH = 17


def _label(text: str, width: int = LABEL_WIDTH) -> str:
    """라벨을 터미널 표시 폭에 맞춰 정렬한다(한글 등 전각 문자는 2칸)."""

    display = sum(
        2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        for character in text
    )
    return text + " " * max(1, width - display) + ":"


def cmd_start(args: argparse.Namespace) -> int:
    """launchd 아래에서 시작한다. 멱등적이다: 실행 중인 백엔드는 건드리지 않는다."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE

    owner = running_pid(settings)
    if agent.is_running():
        suffix = f" (pid {owner})" if owner else ""
        print(f"sesametok-backend가 이미 실행 중입니다{suffix}. 할 일이 없습니다.")
        return EXIT_OK
    if owner is not None:
        # launchd가 관리하지 않는데 락을 잡고 있는 프로세스는 고아다(bootout이 앱 스텁만
        # 정지시킨 경우). 정리하지 않으면 start가 영원히 "이미 실행 중"으로 복구를 거부한다.
        if not terminate_process(owner, timeout=STOP_TIMEOUT_SECONDS):
            print(
                f"sesametok-backend: 이전 프로세스 {owner}이(가) 남아 있어 시작할 수 없습니다.",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        print(f"이전에 남은 프로세스 {owner}을(를) 정리했습니다.")

    if not agent.plist_path.exists():
        agent.install_plist()
        print(f"LaunchAgent를 설치했습니다: {agent.plist_path}")

    result = agent.kickstart() if agent.is_loaded() else agent.bootstrap()
    if not result.ok:
        print(
            f"sesametok-backend: launchctl이 에이전트를 시작하지 못했습니다: {result.message}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    if not wait_until_running(agent, timeout=START_TIMEOUT_SECONDS):
        print(
            "sesametok-backend: LaunchAgent가 로드되었지만 시작하지 않았습니다. "
            "자세한 내용은 `sesametok-backend logs`와 `launchctl print`를 확인하세요.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    started = agent.status()
    suffix = f" (pid {started.pid})" if started.pid else ""
    print(f"sesametok-backend가 시작되었습니다{suffix}.")
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    """``launchctl bootout``으로 중지하므로 ``KeepAlive``가 중지를 방해할 수 없다."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE

    owner = running_pid(settings)
    # 실제로 등록한 plist에 대해서만 launchd에 묻는다.
    loaded = agent.is_loaded() if agent.plist_path.exists() else False
    if not loaded and owner is None:
        print("sesametok-backend가 실행 중이 아닙니다.")
        return EXIT_OK

    if loaded:
        result = agent.bootout()
        if not result.ok and not looks_not_found(result.message):
            print(
                f"sesametok-backend: launchctl bootout에 실패했습니다: {result.message}",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        if not wait_until_stopped(agent, timeout=STOP_TIMEOUT_SECONDS):
            print(
                "sesametok-backend: LaunchAgent가 아직 실행 중입니다; 다시 시도하세요.",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        # bootout은 잡의 main 프로세스(앱 스텁)만 정지시킨다. python 자식이 고아로 남으면
        # 여기서 정리한다(실측: 이 정리가 없으면 API와 폴러가 계속 살아 있었다).
        leftover = running_pid(settings)
        if leftover is not None and not terminate_process(
            leftover, timeout=STOP_TIMEOUT_SECONDS
        ):
            print(
                f"sesametok-backend: 남은 프로세스 {leftover}을(를) 중지하지 못했습니다.",
                file=sys.stderr,
            )
            return EXIT_FAILURE
    else:
        # 개발용 `foreground` 실행은 launchd가 관리하지 않는다; 잠금은 우리 것이다.
        if not terminate_process(owner, timeout=STOP_TIMEOUT_SECONDS):
            print(
                f"sesametok-backend: 프로세스 {owner}이(가) 중지되지 않았습니다.",
                file=sys.stderr,
            )
            return EXIT_FAILURE

    print("sesametok-backend가 중지되었습니다.")
    return EXIT_OK


def cmd_restart(args: argparse.Namespace) -> int:
    """bootout한 뒤 bootstrap을 한 번만 — 살아 있는 인스턴스가 둘이 되지 않도록."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE
    status = agent.status()

    if status.loaded:
        result = agent.bootout()
        if not result.ok and not looks_not_found(result.message):
            print(
                f"sesametok-backend: launchctl bootout에 실패했습니다: {result.message}",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        wait_until_stopped(agent, timeout=STOP_TIMEOUT_SECONDS)

    owner = running_pid(settings)
    if owner is not None:
        terminate_process(owner, timeout=STOP_TIMEOUT_SECONDS)

    if not agent.plist_path.exists():
        agent.install_plist()

    result = agent.bootstrap()
    if not result.ok and not agent.is_loaded():
        print(
            f"sesametok-backend: launchctl이 에이전트를 시작하지 못했습니다: {result.message}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    if not wait_until_running(agent, timeout=START_TIMEOUT_SECONDS):
        print(
            "sesametok-backend: LaunchAgent가 로드되었지만 시작하지 않았습니다.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    started = agent.status()
    suffix = f" (pid {started.pid})" if started.pid else ""
    print(f"sesametok-backend가 재시작되었습니다{suffix}.")
    return EXIT_OK


def cmd_register(args: argparse.Namespace) -> int:
    """login 항목에 등록한다: plist를 쓰고 bootstrap한 뒤 TCC 권한 부여 대상을 안내한다."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE
    path = agent.install_plist()
    print(f"로그인 시 자동시작 plist를 기록했습니다: {path}")

    result = agent.bootstrap()
    if not result.ok and not agent.is_loaded():
        print(
            f"sesametok-backend: launchctl bootstrap에 실패했습니다: {result.message}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    print("로그인 시 자동시작을 켰습니다 (RunAtLoad + KeepAlive).")

    print()
    print("macOS 권한은 책임 프로세스에 귀속됩니다. 앱 번들이 있으면 **앱 하나에만** 주면 되고,")
    print("공용 인터프리터는 아무 권한도 얻지 않습니다 — Terminal도 아닙니다.")
    target = settings.tcc_grant_target
    kind = "앱" if settings.app_installed() else "실행 파일(앱을 먼저 빌드하면 앱에 줄 수 있습니다)"
    print(f"  권한 부여 대상({kind}): {target}")
    print("  시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용   (발신에 필요)")
    print("  (전체 디스크 접근 권한은 보통 필요하지 않습니다 — 읽기가 실패할 때만 주세요)")
    print("  (`sesametok-backend permissions --accessibility`가 해당 창을 엽니다)")
    return EXIT_OK


def cmd_unregister(args: argparse.Namespace) -> int:
    """login 항목에서 해제한다: bootout하고 plist를 제거한 뒤 남은 것을 알린다."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE
    status = agent.status()

    if status.loaded:
        result = agent.bootout()
        if not result.ok and not looks_not_found(result.message):
            print(
                f"sesametok-backend: launchctl bootout에 실패했습니다: {result.message}",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        wait_until_stopped(agent, timeout=STOP_TIMEOUT_SECONDS)

    try:
        removed = agent.remove_plist()
    except OSError as exc:
        print(f"sesametok-backend: {agent.plist_path}을(를) 제거하지 못했습니다: {exc}",
              file=sys.stderr)
        return EXIT_FAILURE

    print(
        f"로그인 시 자동시작 등록을 해제했습니다: {agent.plist_path}"
        if removed
        else f"로그인 시 자동시작이 등록되어 있지 않았습니다: {agent.plist_path}"
    )
    print(f"데이터는 그대로 두었습니다: {settings.data_dir}")
    print(f"로그는 그대로 두었습니다: {settings.log_dir}")
    print("(암호화 아카이브는 그대로 있습니다; 정말로 깨끗한 상태를 원할 때만 "
          "데이터 디렉터리를 직접 삭제하세요)")
    return EXIT_OK


def cmd_archive_key(args: argparse.Namespace) -> int:
    """사용자가 백업할 수 있도록 아카이브 키를 보여준다 (ADR-004)."""

    settings = Settings.from_env()
    try:
        resolved = resolve_archive_key(settings)
    except ArchiveKeyError as exc:
        print(f"sesametok-backend: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    reveal = args.force or sys.stdout.isatty()
    if not reveal:
        print(
            "비대화형 출력에는 아카이브 키를 출력하지 않습니다 "
            "(로그에 남게 됩니다); 터미널에서 다시 실행하거나 --force를 넘기세요",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE

    print(f"출처: {resolved.source}", file=sys.stderr)
    print(KEY_BACKUP_HELP, file=sys.stderr)
    print(key_to_hex(resolved.key))
    return EXIT_OK


def _backend_permissions(settings: Settings) -> dict | None:
    """실행 중인 백엔드가 **스스로 잰** 권한 상태. 못 물으면 ``None``.

    권한은 프로세스마다 다르다. 앱이 띄운 백엔드는 손쉬운 사용을 물려받았지만 이 CLI를
    실행한 터미널은 그렇지 않다. 그래서 백엔드가 살아 있으면 그쪽 판정을 우선한다.
    """

    import urllib.request

    try:
        token = load_or_create_token(settings)
        request = urllib.request.Request(
            f"http://{settings.host}:{settings.port}/v1/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    permissions = payload.get("permissions")
    return permissions if isinstance(permissions, dict) else None


def cmd_status(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    pid = running_pid(settings)
    report = diagnostics.collect(settings, store.store_roots())
    # 실행 중인 백엔드가 있으면 그 프로세스의 권한 판정을 쓴다(터미널과 다를 수 있다).
    live = _backend_permissions(settings) if pid is not None else None
    permission_source = "터미널"
    if live:
        permission_source = "백엔드"
        for key in (
            "accessibility_trusted",
            "store_readable",
            "full_disk_access",
            "grant_target",
            "grant_target_is_app",
        ):
            if key in live:
                report[key] = live[key]
    agent = _launch_agent(settings)
    plist_installed = agent.plist_path.exists()
    # 읽기 전용 조회이며, 살펴봐도 되는 경로에만 수행한다.
    if plist_installed and plist_path_is_allowed(agent.plist_path):
        launchd = agent.status()
    else:
        launchd = LaunchdStatus(loaded=False, running=False)
    payload = {
        "version": __version__,
        "running": pid is not None,
        "pid": pid,
        "token_source": token_source(settings),
        "config": settings.redacted(),
        "permissions": report,
        "launchd": {
            "installed": plist_installed,
            "loaded": launchd.loaded,
            "running": launchd.running,
            "pid": launchd.pid,
        },
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not plist_installed:
        launchd_line = "등록되지 않음"
    elif launchd.running:
        launchd_line = "실행 중" + (f" (pid {launchd.pid})" if launchd.pid else "")
    elif launchd.loaded:
        launchd_line = f"로드됨, 실행 중 아님 ({launchd.state or '알 수 없음'})"
    else:
        launchd_line = "로드되었지만 사용할 수 없음"

    print(f"sesametok-backend {__version__}")
    print(f"{_label('상태')} {'실행 중' if pid is not None else '중지됨'}"
          + (f" (pid {pid})" if pid else ""))
    print(f"{_label('launchd')} {launchd_line}")
    print(f"{_label('API 토큰')} {payload['token_source']}")
    print(f"{_label('데이터 디렉터리')} {settings.data_dir}")
    print(f"{_label('로그 디렉터리')} {settings.log_dir}")
    print(f"{_label('카카오톡')} "
          + ("실행 중" if report["kakaotalk_running"] else
             ("설치됨, 실행 중 아님" if report["kakaotalk_installed"] else "설치되지 않음")))
    print(f"{_label('카카오톡 읽기')} {'가능' if report['store_readable'] else '불가'}")
    fda = report.get("full_disk_access")
    print(f"{_label('전체 디스크 접근')} "
          + ("부여됨" if fda else ("부여 안 됨" if fda is False else "알 수 없음")))
    ax = report["accessibility_trusted"]
    print(f"{_label('손쉬운 사용')} {'정상' if ax else ('없음' if ax is False else '알 수 없음')}"
          + f"   (권한 측정: {permission_source})")
    for line in diagnostics.permission_guidance(report):
        print()
        print(line)
    return EXIT_OK


def cmd_token(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    token = load_or_create_token(settings)
    if args.copy:
        try:
            subprocess.run(["pbcopy"], input=token.encode("utf-8"), check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"sesametok-backend: 클립보드에 복사하지 못했습니다: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        print("API 토큰을 클립보드에 복사했습니다.")
        return EXIT_OK
    print(token)
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    path = settings.backend_log_path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        print(f"아직 로그 파일이 없습니다: {path}", file=sys.stderr)
        return EXIT_FAILURE
    except OSError as exc:
        print(f"sesametok-backend: {path}을(를) 읽지 못했습니다: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    for line in lines[-args.lines :]:
        print(line)
    return EXIT_OK


def cmd_permissions(args: argparse.Namespace) -> int:
    url = (
        diagnostics.ACCESSIBILITY_URL
        if args.accessibility
        else diagnostics.FULL_DISK_ACCESS_URL
    )
    try:
        subprocess.run(["open", url], check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"sesametok-backend: 시스템 설정을 열지 못했습니다: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    settings = Settings.from_env()
    target = settings.tcc_grant_target
    kind = "앱" if settings.app_installed() else "실행 파일"
    print(f"시스템 설정을 열었습니다. 다음 {kind}에 권한을 부여하세요:")
    print(f"  {target}")
    if not settings.app_installed():
        print("  (앱을 빌드하면 권한을 앱 하나에만 줄 수 있습니다: "
              "packaging/build_app.py)")
    return EXIT_OK


def cmd_pending(args: argparse.Namespace) -> int:  # pragma: no cover - 도달 불가
    print(f"{args.command}: 등록된 핸들러가 없습니다", file=sys.stderr)
    return EXIT_UNAVAILABLE


# --------------------------------------------------------------------------- 파서


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sesametok-backend",
        description="로컬 전용 카카오톡 메시징 백엔드 (macOS, 네이티브).",
    )
    parser.add_argument("--version", action="version", version=f"sesametok-backend {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="백엔드를 시작합니다 (터미널을 닫아도 유지됨)")
    start.set_defaults(handler=cmd_start)

    stop = sub.add_parser("stop", help="백엔드를 중지합니다")
    stop.set_defaults(handler=cmd_stop)

    restart = sub.add_parser("restart", help="백엔드를 재시작합니다")
    restart.set_defaults(handler=cmd_restart)

    register = sub.add_parser(
        "register", help="로그인 시 자동시작을 등록합니다 (launchd LaunchAgent)"
    )
    register.set_defaults(handler=cmd_register)

    unregister = sub.add_parser(
        "unregister", help="로그인 시 자동시작 등록을 해제하고 백엔드를 중지합니다"
    )
    unregister.set_defaults(handler=cmd_unregister)

    foreground = sub.add_parser("foreground", help="개발용: 이 터미널에서 실행합니다")
    foreground.add_argument(
        "--managed",
        action="store_true",
        help="launchd 아래에서 실행 중 (stderr가 로그 파일이므로 비밀 값을 절대 출력하지 않음)",
    )
    foreground.set_defaults(handler=cmd_foreground)

    archive_key = sub.add_parser(
        "archive-key", help="백업용 아카이브 암호화 키를 보여줍니다"
    )
    archive_key.add_argument(
        "--force", action="store_true", help="터미널에 연결되어 있지 않아도 출력합니다"
    )
    archive_key.set_defaults(handler=cmd_archive_key)

    status = sub.add_parser("status", help="실행/권한/폴링 상태")
    status.add_argument("--json", action="store_true", help="기계가 읽을 수 있는 출력")
    status.set_defaults(handler=cmd_status)

    token = sub.add_parser("token", help="인터페이스 API 토큰을 출력합니다")
    token.add_argument("--copy", action="store_true", help="클립보드에 복사합니다")
    token.set_defaults(handler=cmd_token)

    logs = sub.add_parser("logs", help="백엔드 로그의 끝부분을 출력합니다")
    logs.add_argument("-n", "--lines", type=int, default=50, help="줄 수")
    logs.set_defaults(handler=cmd_logs)

    permissions = sub.add_parser("permissions", help="macOS 권한 설정 창을 엽니다")
    permissions.add_argument(
        "--accessibility", action="store_true", help="전체 디스크 접근 대신 손쉬운 사용 설정 창을 엽니다"
    )
    permissions.set_defaults(handler=cmd_permissions)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", cmd_pending)
    try:
        return handler(args)
    except ConfigError as exc:
        print(f"sesametok-backend: 설정 오류: {exc}", file=sys.stderr)
        return EXIT_CONFIG
