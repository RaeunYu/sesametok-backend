"""Single-instance guard plus launchd LaunchAgent management.

Two backend processes touching the KakaoTalk UI at once can produce real damage,
so the process holds an exclusive ``flock`` on a lock file for its whole
lifetime. The OS releases the lock automatically if the process dies, so a stale
lock can never block a restart.

The same module owns the LaunchAgent this project installs: the plist is rendered
from a template with real absolute paths,
and every ``launchctl`` call is a timeout-bounded ``subprocess`` with no shell.
Read-only queries (``print``) never raise on a nonzero exit — they report
"not loaded" and carry the stderr text instead.
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
# launchd gives a very sparse PATH; the backend shells out to pgrep/open/ioreg.
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: ``launchctl print`` reports a missing service on stderr with one of these.
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
    """Exclusive, self-healing process lock backed by ``flock``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock. Returns ``False`` when another process holds it."""

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
        """PID recorded in the lock file, whether or not we hold the lock."""

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
        """True when another live process currently holds the lock."""

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


# --------------------------------------------------------------- plist content

#: Escape hatch for deliberate alternate installs; never set in tests.
ALLOW_PLIST_OVERRIDE_ENV = "SESAMETOK_ALLOW_PLIST_OVERRIDE"
_TRUTHY = {"1", "true", "yes", "on"}


class PlistPathNotAllowed(RuntimeError):
    """Raised when a plist override would touch a machine-owned location."""


def launch_agents_dir() -> Path:
    """The only non-temporary location this project may manage."""

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
    """True when managing *plist_path* cannot touch a real user's LaunchAgents.

    Allowed are ``~/Library/LaunchAgents`` itself and anything under a temporary
    directory. Anything else needs an explicit ``SESAMETOK_ALLOW_PLIST_OVERRIDE=1``
    so a stray environment variable can never rewrite the wrong plist or hand a
    bogus path to ``launchctl``.
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
    """Raise :class:`PlistPathNotAllowed` unless *plist_path* is safe to manage."""

    if plist_path_is_allowed(plist_path):
        return
    raise PlistPathNotAllowed(
        f"refusing to manage {plist_path}: it is outside "
        f"{launch_agents_dir()} and outside a temporary directory. "
        f"Set {ALLOW_PLIST_OVERRIDE_ENV}=1 to override deliberately."
    )


def render_plist(settings: Settings, *, label: str = LAUNCHD_LABEL) -> str:
    """Render the LaunchAgent XML with the real absolute paths substituted.

    The result is a normal property list: ``plistlib.loads`` must be able to parse
    it. Paths are escaped so an unlucky home directory can never break the XML.
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
    """Write the plist (creating parents and the log dir) and return its path.

    The path is injected rather than read from ``settings`` when given, so tests
    and callers can keep everything under a temporary directory.
    """

    path = Path(plist_path) if plist_path is not None else settings.launchd_plist_path
    require_plist_path_allowed(path)
    ensure_dir(path.parent)
    ensure_dir(settings.log_dir)
    atomic_write_text(path, render_plist(settings, label=label))
    return path


def remove_plist(plist_path: Path) -> bool:
    """Delete *plist_path* if present. Returns ``True`` when a file was removed."""

    path = Path(plist_path)
    if not path.exists():
        return False
    path.unlink()
    return True


# ------------------------------------------------------------------ launchctl


@dataclass(frozen=True)
class LaunchctlResult:
    """Outcome of one ``launchctl`` invocation. Failures are values, not raises."""

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
        """The most useful diagnostic text launchctl produced."""

        return (self.stderr or self.stdout).strip()


#: Runs a full argv (starting with ``launchctl``) and never raises.
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
    """Run one ``launchctl`` command with no shell and a hard timeout."""

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
        # Permission errors and the like surface here rather than as exceptions.
        return LaunchctlResult(tuple(args), 126, "", str(exc))
    return LaunchctlResult(
        tuple(args), completed.returncode, completed.stdout or "", completed.stderr or ""
    )


@dataclass(frozen=True)
class LaunchdStatus:
    """Parsed view of ``launchctl print gui/<uid>/<label>``."""

    loaded: bool
    running: bool
    pid: int | None = None
    state: str | None = None
    error: str = ""


def looks_not_found(text: str) -> bool:
    """True when launchctl's output means "this service does not exist"."""

    lowered = (text or "").lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def parse_launchctl_print(text: str) -> tuple[str | None, int | None]:
    """Return ``(state, pid)`` parsed from ``launchctl print`` output."""

    body = text or ""
    state_match = _STATE_RE.search(body)
    pid_match = _PID_RE.search(body)
    return (
        state_match.group(1) if state_match else None,
        int(pid_match.group(1)) if pid_match else None,
    )


class LaunchAgent:
    """The LaunchAgent this project owns, addressed as ``gui/<uid>/<label>``."""

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

    # ------------------------------------------------------------ plist helpers

    def install_plist(self) -> Path:
        """Write (or rewrite) this agent's plist."""

        return install_plist(self.settings, self.plist_path, label=self.label)

    def remove_plist(self) -> bool:
        """Delete this agent's plist, if present."""

        return remove_plist(self.plist_path)

    # --------------------------------------------------------- launchctl verbs

    def bootstrap(self) -> LaunchctlResult:
        """``launchctl bootstrap gui/<uid> <plist>`` — load and (RunAtLoad) start."""

        return self._run("bootstrap", self.domain, str(self.plist_path))

    def bootout(self) -> LaunchctlResult:
        """``launchctl bootout gui/<uid>/<label>`` — unload; KeepAlive can't fight it."""

        return self._run("bootout", self.service)

    def kickstart(self, *, restart: bool = True) -> LaunchctlResult:
        """``launchctl kickstart -k gui/<uid>/<label>`` — start/restart in place."""

        parts = ["kickstart"]
        if restart:
            parts.append("-k")
        parts.append(self.service)
        return self._run(*parts)

    def print_state(self) -> LaunchctlResult:
        """``launchctl print gui/<uid>/<label>`` — read-only status."""

        return self._run("print", self.service)

    # ------------------------------------------------------------------ status

    def status(self) -> LaunchdStatus:
        """Loaded/running state, never raising on a nonzero exit."""

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


# -------------------------------------------------------------------- waiting


def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.2
) -> bool:
    """Poll *predicate* until it is true or *timeout* elapses (checked once at 0)."""

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
    """True once launchd reports the agent running."""

    return wait_until(agent.is_running, timeout, interval)


def wait_until_stopped(
    agent: LaunchAgent, timeout: float = 5.0, interval: float = 0.2
) -> bool:
    """True once launchd no longer reports the agent running."""

    return wait_until(lambda: not agent.is_running(), timeout, interval)


def process_alive(pid: int) -> bool:
    """True when *pid* exists (used for the single-instance lock owner)."""

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
    """SIGTERM a lock-owning foreground process and wait for it to exit.

    ``bootout`` only stops launchd-managed instances; a development
    ``foreground`` run holds the lock instead. Returns ``True`` when the process
    is gone.
    """

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return wait_until(lambda: not process_alive(pid), timeout, interval)
