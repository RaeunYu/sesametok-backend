"""``sesametok-backend`` command line interface.

``foreground`` runs the process in this terminal; ``start``/``stop``/``restart``/
``install``/``uninstall`` manage the launchd LaunchAgent so the backend survives
closing the terminal and logging back in.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

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

#: How long to wait for launchd to report a lifecycle change.
START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0


def _launch_agent(settings: Settings) -> LaunchAgent:
    """Build the LaunchAgent for *settings* (a seam for tests)."""

    return LaunchAgent(settings)


def _require_managed_plist(agent: LaunchAgent) -> bool:
    """Refuse to write or run ``launchctl`` for a machine-owned plist path."""

    try:
        require_plist_path_allowed(agent.plist_path)
    except PlistPathNotAllowed as exc:
        print(f"sesametok-backend: {exc}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------- commands


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


def cmd_start(args: argparse.Namespace) -> int:
    """Start under launchd. Idempotent: a running backend is left alone."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE

    owner = running_pid(settings)
    if agent.is_running() or owner is not None:
        suffix = f" (pid {owner})" if owner else ""
        print(f"sesametok-backend is already running{suffix}; nothing to do.")
        return EXIT_OK

    if not agent.plist_path.exists():
        agent.install_plist()
        print(f"installed LaunchAgent: {agent.plist_path}")

    result = agent.kickstart() if agent.is_loaded() else agent.bootstrap()
    if not result.ok:
        print(
            f"sesametok-backend: launchctl could not start the agent: {result.message}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    if not wait_until_running(agent, timeout=START_TIMEOUT_SECONDS):
        print(
            "sesametok-backend: the LaunchAgent was loaded but did not start; "
            "check `sesametok-backend logs` and `launchctl print` for details.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    started = agent.status()
    suffix = f" (pid {started.pid})" if started.pid else ""
    print(f"sesametok-backend started{suffix}.")
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop via ``launchctl bootout`` so ``KeepAlive`` cannot fight the stop."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE

    owner = running_pid(settings)
    # Only ask launchd about a plist we actually installed.
    loaded = agent.is_loaded() if agent.plist_path.exists() else False
    if not loaded and owner is None:
        print("sesametok-backend is not running.")
        return EXIT_OK

    if loaded:
        result = agent.bootout()
        if not result.ok and not looks_not_found(result.message):
            print(
                f"sesametok-backend: launchctl bootout failed: {result.message}",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        if not wait_until_stopped(agent, timeout=STOP_TIMEOUT_SECONDS):
            print(
                "sesametok-backend: the LaunchAgent is still running; try again.",
                file=sys.stderr,
            )
            return EXIT_FAILURE
    else:
        # A development `foreground` run is not launchd-managed; the lock is ours.
        if not terminate_process(owner, timeout=STOP_TIMEOUT_SECONDS):
            print(
                f"sesametok-backend: process {owner} did not stop.",
                file=sys.stderr,
            )
            return EXIT_FAILURE

    print("sesametok-backend stopped.")
    return EXIT_OK


def cmd_restart(args: argparse.Namespace) -> int:
    """Bootout then bootstrap once — never two live instances."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE
    status = agent.status()

    if status.loaded:
        result = agent.bootout()
        if not result.ok and not looks_not_found(result.message):
            print(
                f"sesametok-backend: launchctl bootout failed: {result.message}",
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
            f"sesametok-backend: launchctl could not start the agent: {result.message}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    if not wait_until_running(agent, timeout=START_TIMEOUT_SECONDS):
        print(
            "sesametok-backend: the LaunchAgent was loaded but did not start.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    started = agent.status()
    suffix = f" (pid {started.pid})" if started.pid else ""
    print(f"sesametok-backend restarted{suffix}.")
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    """Write the plist, bootstrap it, and explain the TCC grant target."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE
    path = agent.install_plist()
    print(f"LaunchAgent written: {path}")

    result = agent.bootstrap()
    if not result.ok and not agent.is_loaded():
        print(
            f"sesametok-backend: launchctl bootstrap failed: {result.message}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    print("Login autostart enabled (RunAtLoad + KeepAlive).")

    print()
    print("macOS binds Full Disk Access and Accessibility to the responsible")
    print("process, so with a LaunchAgent you must grant them to the executable")
    print("launchd starts — not to Terminal.")
    print(f"  grant target: {diagnostics.python_executable()}")
    print("  System Settings > Privacy & Security > Full Disk Access")
    print("  then System Settings > Privacy & Security > Accessibility")
    print("  (`sesametok-backend permissions` / `--accessibility` open those panes)")
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Bootout, remove the plist, and say what was left on disk."""

    settings = Settings.from_env()
    agent = _launch_agent(settings)
    if not _require_managed_plist(agent):
        return EXIT_FAILURE
    status = agent.status()

    if status.loaded:
        result = agent.bootout()
        if not result.ok and not looks_not_found(result.message):
            print(
                f"sesametok-backend: launchctl bootout failed: {result.message}",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        wait_until_stopped(agent, timeout=STOP_TIMEOUT_SECONDS)

    try:
        removed = agent.remove_plist()
    except OSError as exc:
        print(f"sesametok-backend: could not remove {agent.plist_path}: {exc}",
              file=sys.stderr)
        return EXIT_FAILURE

    print(
        f"LaunchAgent removed: {agent.plist_path}"
        if removed
        else f"LaunchAgent was not installed at {agent.plist_path}."
    )
    print(f"data left untouched: {settings.data_dir}")
    print(f"logs left untouched: {settings.log_dir}")
    print("(the encrypted archive is still there; delete the data dir by hand "
          "only if you really want a clean slate)")
    return EXIT_OK


def cmd_archive_key(args: argparse.Namespace) -> int:
    """Show the archive key so the user can back it up (ADR-004)."""

    settings = Settings.from_env()
    try:
        resolved = resolve_archive_key(settings)
    except ArchiveKeyError as exc:
        print(f"sesametok-backend: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    reveal = args.force or sys.stdout.isatty()
    if not reveal:
        print(
            "refusing to print the archive key to a non-interactive output "
            "(it would end up in a log); re-run in a terminal or pass --force",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE

    print(f"source: {resolved.source}", file=sys.stderr)
    print(KEY_BACKUP_HELP, file=sys.stderr)
    print(key_to_hex(resolved.key))
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    pid = running_pid(settings)
    report = diagnostics.collect(settings, store.store_roots())
    agent = _launch_agent(settings)
    plist_installed = agent.plist_path.exists()
    # Read-only probe, and only for a path we are allowed to look at.
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
        launchd_line = "not installed"
    elif launchd.running:
        launchd_line = "running" + (f" (pid {launchd.pid})" if launchd.pid else "")
    elif launchd.loaded:
        launchd_line = f"loaded, not running ({launchd.state or 'unknown'})"
    else:
        launchd_line = "loaded but unavailable"

    print(f"sesametok-backend {__version__}")
    print(f"state          : {'running' if pid is not None else 'stopped'}"
          + (f" (pid {pid})" if pid else ""))
    print(f"launchd        : {launchd_line}")
    print(f"api token      : {payload['token_source']}")
    print(f"data dir       : {settings.data_dir}")
    print(f"log dir        : {settings.log_dir}")
    print(f"kakaotalk      : "
          + ("running" if report["kakaotalk_running"] else
             ("installed, not running" if report["kakaotalk_installed"] else "not installed")))
    print(f"full disk access: {'ok' if report['full_disk_access_ok'] else 'missing'}")
    ax = report["accessibility_trusted"]
    print(f"accessibility  : {'ok' if ax else ('missing' if ax is False else 'unknown')}")
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
            print(f"sesametok-backend: could not copy to clipboard: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        print("API token copied to the clipboard.")
        return EXIT_OK
    print(token)
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    path = settings.backend_log_path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        print(f"no log file yet at {path}", file=sys.stderr)
        return EXIT_FAILURE
    except OSError as exc:
        print(f"sesametok-backend: could not read {path}: {exc}", file=sys.stderr)
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
        print(f"sesametok-backend: could not open System Settings: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    print("Opened System Settings. Grant permission to:")
    print(f"  {diagnostics.python_executable()}")
    return EXIT_OK


def cmd_pending(args: argparse.Namespace) -> int:  # pragma: no cover - unreachable
    print(f"{args.command}: no handler is registered", file=sys.stderr)
    return EXIT_UNAVAILABLE


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sesametok-backend",
        description="Local-only KakaoTalk messaging backend (macOS, native).",
    )
    parser.add_argument("--version", action="version", version=f"sesametok-backend {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start the backend (survives closing the terminal)")
    start.set_defaults(handler=cmd_start)

    stop = sub.add_parser("stop", help="stop the backend")
    stop.set_defaults(handler=cmd_stop)

    restart = sub.add_parser("restart", help="restart the backend")
    restart.set_defaults(handler=cmd_restart)

    install = sub.add_parser("install", help="register the LaunchAgent (start at login)")
    install.set_defaults(handler=cmd_install)

    uninstall = sub.add_parser(
        "uninstall", help="unregister the LaunchAgent and stop the backend"
    )
    uninstall.set_defaults(handler=cmd_uninstall)

    foreground = sub.add_parser("foreground", help="development: run in this terminal")
    foreground.add_argument(
        "--managed",
        action="store_true",
        help="running under launchd (stderr is a log file; never print secrets)",
    )
    foreground.set_defaults(handler=cmd_foreground)

    archive_key = sub.add_parser(
        "archive-key", help="show the archive encryption key for backup"
    )
    archive_key.add_argument(
        "--force", action="store_true", help="print even when not attached to a terminal"
    )
    archive_key.set_defaults(handler=cmd_archive_key)

    status = sub.add_parser("status", help="run/permission/polling state")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(handler=cmd_status)

    token = sub.add_parser("token", help="print the interface API token")
    token.add_argument("--copy", action="store_true", help="copy it to the clipboard")
    token.set_defaults(handler=cmd_token)

    logs = sub.add_parser("logs", help="tail the backend log")
    logs.add_argument("-n", "--lines", type=int, default=50, help="number of lines")
    logs.set_defaults(handler=cmd_logs)

    permissions = sub.add_parser("permissions", help="open the macOS permission pane")
    permissions.add_argument(
        "--accessibility", action="store_true", help="open Accessibility instead of Full Disk Access"
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
        print(f"sesametok-backend: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
