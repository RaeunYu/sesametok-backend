"""단일 인스턴스 가드와 launchd LaunchAgent 관리.

백엔드 프로세스 둘이 동시에 카카오톡 UI를 건드리면 실제 손상이 생길 수 있으므로,
프로세스는 수명 전체 동안 잠금 파일에 배타적 ``flock``을 건다. 프로세스가 죽으면
OS가 잠금을 자동으로 풀어 주므로 낡은 잠금이 재시작을 막을 수 없다.

이 모듈은 이 프로젝트가 설치하는 LaunchAgent도 소유한다: plist는 실제 절대 경로로
템플릿에서 렌더링되고, 모든 ``launchctl`` 호출은 셸 없이 타임아웃이 걸린
``subprocess``다. 읽기 전용 조회(``print``)는 0이 아닌 종료 코드에서 예외를
일으키지 않고 "로드되지 않음"을 보고하며 stderr 텍스트를 대신 담는다.
"""

from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from .config import LAUNCHD_LABEL, Settings
from .fsutil import atomic_write_text, ensure_dir, ensure_private_dir

MODULE_NAME = "sesametok_backend"
LAUNCHCTL = "launchctl"
LAUNCHCTL_TIMEOUT_SECONDS = 10.0
# launchd는 PATH를 아주 빈약하게 주므로, 백엔드는 pgrep/open/ioreg를 셸로 호출한다.
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: ``launchctl print``는 없는 서비스를 stderr에서 이 중 하나로 보고한다.
_NOT_FOUND_MARKERS = (
    "could not find service",
    "service not found",
    "no such process",
    "bad request",
)

_STATE_RE = re.compile(r"^\s*state\s*=\s*(\S+)\s*$", re.MULTILINE)
_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string><string>{module}</string>
    <string>foreground</string><string>--managed</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>{stdout}</string>
  <key>StandardErrorPath</key><string>{stderr}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path}</string>
  </dict>
</dict></plist>
"""


class ProcessLock:
    """``flock`` 기반의 배타적이고 스스로 복구되는 프로세스 잠금."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """잠금을 잡아 본다. 다른 프로세스가 쥐고 있으면 ``False``를 반환한다."""

        if self._fd is not None:
            return True
        ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except OSError:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def owner_pid(self) -> int | None:
        """우리가 잠금을 쥐고 있든 아니든, 잠금 파일에 기록된 PID."""

        try:
            text = self.path.read_text(encoding="ascii").strip()
        except OSError:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    @staticmethod
    def is_held(path: Path) -> bool:
        """다른 살아 있는 프로세스가 현재 잠금을 쥐고 있으면 True."""

        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)
        return False


# -------------------------------------------------------------------- plist 내용

#: 의도적인 대체 설치를 위한 탈출구이며, 테스트에서는 절대 설정하지 않는다.
ALLOW_PLIST_OVERRIDE_ENV = "SESAMETOK_ALLOW_PLIST_OVERRIDE"
_TRUTHY = {"1", "true", "yes", "on"}


class PlistPathNotAllowed(RuntimeError):
    """plist 재정의가 머신이 소유한 위치를 건드리게 될 때 발생한다."""


def launch_agents_dir() -> Path:
    """이 프로젝트가 관리해도 되는 유일한 비임시 위치."""

    return Path.home() / "Library" / "LaunchAgents"


def _temp_roots() -> tuple[Path, ...]:
    roots = {
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/folders"),
        Path("/private/var/folders"),
    }
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            roots.add(Path(value))
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            continue
    return tuple(resolved)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def plist_path_is_allowed(plist_path: Path) -> bool:
    """*plist_path*를 관리해도 실제 사용자의 LaunchAgents를 건드리지 않으면 True.

    ``~/Library/LaunchAgents`` 자체와 임시 디렉터리 아래의 모든 경로가 허용된다.
    그 밖의 경로에는 명시적인 ``SESAMETOK_ALLOW_PLIST_OVERRIDE=1``이 필요하므로,
    엉뚱한 환경변수가 잘못된 plist를 다시 쓰거나 ``launchctl``에 가짜 경로를 넘길
    수 없다.
    """

    if os.environ.get(ALLOW_PLIST_OVERRIDE_ENV, "").strip().lower() in _TRUTHY:
        return True
    try:
        path = Path(plist_path).expanduser().resolve()
        agents = launch_agents_dir().resolve()
    except OSError:
        return False
    if _is_within(path, agents):
        return True
    return any(_is_within(path, root) for root in _temp_roots())


def require_plist_path_allowed(plist_path: Path) -> None:
    """*plist_path*가 관리해도 안전하지 않으면 :class:`PlistPathNotAllowed`를 발생시킨다."""

    if plist_path_is_allowed(plist_path):
        return
    raise PlistPathNotAllowed(
        f"{plist_path} 관리를 거부합니다: 이 경로는 "
        f"{launch_agents_dir()} 밖이고 임시 디렉터리 밖에 있습니다. "
        f"의도적으로 재정의하려면 {ALLOW_PLIST_OVERRIDE_ENV}=1을 설정하세요."
    )


def render_plist(settings: Settings, *, label: str = LAUNCHD_LABEL) -> str:
    """실제 절대 경로를 채워 LaunchAgent XML을 렌더링한다.

    결과는 평범한 property list이며, ``plistlib.loads``가 파싱할 수 있어야 한다.
    경로는 이스케이프되므로 운 나쁜 홈 디렉터리가 XML을 깨뜨릴 수 없다.
    """

    return PLIST_TEMPLATE.format(
        label=_xml_escape(label),
        python=_xml_escape(str(settings.python_executable)),
        module=MODULE_NAME,
        stdout=_xml_escape(str(settings.log_dir / "out.log")),
        stderr=_xml_escape(str(settings.log_dir / "err.log")),
        path=LAUNCHD_PATH,
    )


def install_plist(
    settings: Settings,
    plist_path: Path | None = None,
    *,
    label: str = LAUNCHD_LABEL,
) -> Path:
    """plist를 쓰고(상위 디렉터리와 로그 디렉터리도 생성) 그 경로를 반환한다.

    경로를 주면 ``settings``에서 읽지 않고 주입된 값을 쓰므로, 테스트와 호출자가
    모든 것을 임시 디렉터리 아래에 둘 수 있다.
    """

    path = Path(plist_path) if plist_path is not None else settings.launchd_plist_path
    require_plist_path_allowed(path)
    ensure_dir(path.parent)
    ensure_dir(settings.log_dir)
    atomic_write_text(path, render_plist(settings, label=label))
    return path


def remove_plist(plist_path: Path) -> bool:
    """*plist_path*가 있으면 삭제한다. 파일을 제거했으면 ``True``를 반환한다."""

    path = Path(plist_path)
    if not path.exists():
        return False
    path.unlink()
    return True


# ------------------------------------------------------------------ launchctl


@dataclass(frozen=True)
class LaunchctlResult:
    """``launchctl`` 호출 한 번의 결과. 실패는 예외가 아니라 값이다."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def message(self) -> str:
        """launchctl이 내놓은 가장 유용한 진단 텍스트."""

        return (self.stderr or self.stdout).strip()


#: 전체 argv(``launchctl``로 시작)를 실행하며 절대 예외를 일으키지 않는다.
Runner = Callable[[Sequence[str], float], LaunchctlResult]


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def run_launchctl(
    argv: Sequence[str], timeout: float = LAUNCHCTL_TIMEOUT_SECONDS
) -> LaunchctlResult:
    """셸 없이 엄격한 타임아웃으로 ``launchctl`` 명령 하나를 실행한다."""

    args = [str(part) for part in argv]
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return LaunchctlResult(
            tuple(args), 124, _decode(exc.stdout), _decode(exc.stderr), timed_out=True
        )
    except FileNotFoundError as exc:
        return LaunchctlResult(tuple(args), 127, "", str(exc))
    except OSError as exc:
        # 권한 오류 같은 것이 예외 대신 여기로 드러난다.
        return LaunchctlResult(tuple(args), 126, "", str(exc))
    return LaunchctlResult(
        tuple(args), completed.returncode, completed.stdout or "", completed.stderr or ""
    )


@dataclass(frozen=True)
class LaunchdStatus:
    """``launchctl print gui/<uid>/<label>``의 파싱된 뷰."""

    loaded: bool
    running: bool
    pid: int | None = None
    state: str | None = None
    error: str = ""


def looks_not_found(text: str) -> bool:
    """launchctl의 출력이 "이 서비스는 존재하지 않는다"를 뜻하면 True."""

    lowered = (text or "").lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def parse_launchctl_print(text: str) -> tuple[str | None, int | None]:
    """``launchctl print`` 출력에서 파싱한 ``(state, pid)``를 반환한다."""

    body = text or ""
    state_match = _STATE_RE.search(body)
    pid_match = _PID_RE.search(body)
    return (
        state_match.group(1) if state_match else None,
        int(pid_match.group(1)) if pid_match else None,
    )


class LaunchAgent:
    """이 프로젝트가 소유하는 LaunchAgent이며, ``gui/<uid>/<label>``로 지칭한다."""

    def __init__(
        self,
        settings: Settings,
        plist_path: Path | None = None,
        *,
        label: str = LAUNCHD_LABEL,
        uid: int | None = None,
        runner: Runner | None = None,
        timeout: float = LAUNCHCTL_TIMEOUT_SECONDS,
    ) -> None:
        self.settings = settings
        self.plist_path = (
            Path(plist_path) if plist_path is not None else settings.launchd_plist_path
        )
        self.label = label
        self.uid = os.getuid() if uid is None else uid
        self.timeout = timeout
        self._runner: Runner = runner or run_launchctl
        self.last_error: str = ""

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def service(self) -> str:
        return f"gui/{self.uid}/{self.label}"

    def _run(self, *parts: str) -> LaunchctlResult:
        result = self._runner((LAUNCHCTL, *parts), self.timeout)
        self.last_error = "" if result.ok else result.message
        return result

    # ----------------------------------------------------------------- plist 헬퍼

    def install_plist(self) -> Path:
        """이 에이전트의 plist를 쓴다(또는 다시 쓴다)."""

        return install_plist(self.settings, self.plist_path, label=self.label)

    def remove_plist(self) -> bool:
        """이 에이전트의 plist가 있으면 삭제한다."""

        return remove_plist(self.plist_path)

    # ------------------------------------------------------------ launchctl 명령

    def bootstrap(self) -> LaunchctlResult:
        """``launchctl bootstrap gui/<uid> <plist>`` — 로드하고 (RunAtLoad로) 시작한다."""

        return self._run("bootstrap", self.domain, str(self.plist_path))

    def bootout(self) -> LaunchctlResult:
        """``launchctl bootout gui/<uid>/<label>`` — 언로드한다; KeepAlive가 방해할 수 없다."""

        return self._run("bootout", self.service)

    def kickstart(self, *, restart: bool = True) -> LaunchctlResult:
        """``launchctl kickstart -k gui/<uid>/<label>`` — 제자리에서 시작/재시작한다."""

        parts = ["kickstart"]
        if restart:
            parts.append("-k")
        parts.append(self.service)
        return self._run(*parts)

    def print_state(self) -> LaunchctlResult:
        """``launchctl print gui/<uid>/<label>`` — 읽기 전용 상태."""

        return self._run("print", self.service)

    # ---------------------------------------------------------------------- 상태

    def status(self) -> LaunchdStatus:
        """로드/실행 상태이며, 0이 아닌 종료 코드에서도 절대 예외를 일으키지 않는다."""

        result = self.print_state()
        if not result.ok or looks_not_found(result.message):
            return LaunchdStatus(loaded=False, running=False, error=result.message)
        state, pid = parse_launchctl_print(result.stdout)
        return LaunchdStatus(
            loaded=True,
            running=state == "running",
            pid=pid,
            state=state,
            error=result.message,
        )

    def is_loaded(self) -> bool:
        return self.status().loaded

    def is_running(self) -> bool:
        return self.status().running


# ------------------------------------------------------------------------- 대기


def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.2
) -> bool:
    """*predicate*가 참이 되거나 *timeout*이 지날 때까지 폴링한다 (0에서 한 번 확인)."""

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, interval))


def wait_until_running(
    agent: LaunchAgent, timeout: float = 5.0, interval: float = 0.2
) -> bool:
    """launchd가 에이전트 실행을 보고하면 True."""

    return wait_until(agent.is_running, timeout, interval)


def wait_until_stopped(
    agent: LaunchAgent, timeout: float = 5.0, interval: float = 0.2
) -> bool:
    """launchd가 더 이상 에이전트 실행을 보고하지 않으면 True."""

    return wait_until(lambda: not agent.is_running(), timeout, interval)


def process_alive(pid: int) -> bool:
    """*pid*가 존재하면 True (단일 인스턴스 잠금 소유자에 사용)."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_process(pid: int, timeout: float = 5.0, interval: float = 0.1) -> bool:
    """잠금을 소유한 foreground 프로세스에 SIGTERM을 보내고 종료를 기다린다.

    ``bootout``은 launchd가 관리하는 인스턴스만 중지한다; 개발용 ``foreground``
    실행은 대신 잠금을 쥐고 있다. 프로세스가 사라지면 ``True``를 반환한다.
    """

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return wait_until(lambda: not process_alive(pid), timeout, interval)
