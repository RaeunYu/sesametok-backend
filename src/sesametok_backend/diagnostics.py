"""Runtime diagnosis: does this process have what it needs to work?

Two macOS permissions matter and both are bound to the responsible executable's
absolute path:

* *Full Disk Access* — needed to read the KakaoTalk container database.
* *Accessibility* — needed to drive the KakaoTalk UI when sending.

The first-run guide prints exactly which executable must be granted, so nobody
has to guess.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import Settings

KAKAOTALK_BUNDLE = "KakaoTalk"
KAKAOTALK_APP = Path("/Applications/KakaoTalk.app")

FULL_DISK_ACCESS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
)
ACCESSIBILITY_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


def python_executable() -> str:
    """Absolute path of the binary TCC actually binds to — the grant target.

    A venv created from a pyenv interpreter symlinks to a *version-pinned* path,
    so resolving matters: granting the venv path still stores the resolved target,
    and a Python upgrade silently invalidates the grant.
    """

    return str(Path(sys.executable).resolve())


def invoked_executable() -> str:
    """The interpreter path as launched (e.g. ``<venv>/bin/python``)."""

    return str(Path(sys.executable))


def python_is_symlinked() -> bool:
    """True when the invoked interpreter resolves to a different path."""

    return invoked_executable() != python_executable()


def kakaotalk_installed() -> bool:
    return KAKAOTALK_APP.is_dir()


def kakaotalk_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", KAKAOTALK_BUNDLE],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def accessibility_trusted() -> bool | None:
    """``True``/``False`` when the framework answers, ``None`` when unavailable."""

    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return bool(AXIsProcessTrusted())
    except Exception:
        return None


def path_readable(path: Path) -> bool:
    try:
        os.listdir(path)
    except OSError:
        return False
    return True


def store_root_report(roots: Iterable[Path]) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for root in roots:
        report.append(
            {
                "path": str(root),
                "exists": root.is_dir(),
                "readable": path_readable(root) if root.is_dir() else False,
            }
        )
    return report


def full_disk_access_ok(roots: Iterable[Path]) -> bool:
    """True when at least one existing store root can be listed."""

    existing = [root for root in roots if root.is_dir()]
    if not existing:
        return False
    return any(path_readable(root) for root in existing)


def collect(settings: Settings, store_roots: Iterable[Path]) -> dict[str, Any]:
    """Everything the first-run guide and ``status`` need, with no personal data."""

    roots = tuple(store_roots)
    return {
        "python_executable": python_executable(),
        "invoked_executable": invoked_executable(),
        "python_is_symlinked": python_is_symlinked(),
        "kakaotalk_installed": kakaotalk_installed(),
        "kakaotalk_running": kakaotalk_running(),
        "accessibility_trusted": accessibility_trusted(),
        "full_disk_access_ok": full_disk_access_ok(roots),
        "store_roots": store_root_report(roots),
        "menubar": not settings.no_menubar,
    }


def permission_guidance(report: dict[str, Any]) -> list[str]:
    """Human-readable next steps for whatever permission is missing."""

    exe = report.get("python_executable", "?")
    lines: list[str] = []
    if report.get("python_is_symlinked"):
        lines.append(
            "This interpreter is a symlink; macOS records the resolved binary for "
            "permissions.\n"
            f"  launched as: {report.get('invoked_executable', '?')}\n"
            f"  grant target: {exe}\n"
            "  Upgrading that Python build invalidates the grant — recreate the venv "
            "and re-grant if you switch versions."
        )
    if not report.get("full_disk_access_ok"):
        lines.append(
            "Full Disk Access is required to read the KakaoTalk database.\n"
            f"  Grant it to this executable: {exe}\n"
            "  System Settings > Privacy & Security > Full Disk Access\n"
            "  (run `sesametok-backend permissions` to open that pane)"
        )
    if report.get("accessibility_trusted") is False:
        lines.append(
            "Accessibility is required to send messages through the KakaoTalk UI.\n"
            f"  Grant it to this executable: {exe}\n"
            "  System Settings > Privacy & Security > Accessibility\n"
            "  (run `sesametok-backend permissions --accessibility`)"
        )
    if not report.get("kakaotalk_installed"):
        lines.append("KakaoTalk.app was not found in /Applications.")
    elif not report.get("kakaotalk_running"):
        lines.append("KakaoTalk is installed but not running; no new messages will appear.")
    return lines
