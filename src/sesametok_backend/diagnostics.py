"""런타임 진단: 이 프로세스가 동작에 필요한 것을 갖추었는지 확인한다.

macOS 권한 두 가지가 중요하며, 둘 다 책임 프로세스의 절대 경로에 귀속된다:

* *전체 디스크 접근 권한* — 카카오톡 컨테이너 DB를 읽는 데 필요하다.
* *손쉬운 사용* — 발신할 때 카카오톡 UI를 구동하는 데 필요하다.

첫 실행 안내는 **어느 실행 파일에 권한을 줘야 하는지** 그대로 출력한다. 추측할 필요가 없도록.
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
    """TCC가 실제로 귀속되는 바이너리의 절대 경로 — 권한 부여 대상.

    pyenv 인터프리터로 만든 venv는 *버전이 고정된* 경로를 가리키는 심볼릭 링크다.
    그래서 resolve가 중요하다: venv 경로에 권한을 줘도 저장되는 것은 resolve된 대상이고,
    Python을 업그레이드하면 권한이 조용히 무효가 된다.
    """

    return str(Path(sys.executable).resolve())


def invoked_executable() -> str:
    """실행에 사용된 인터프리터 경로(예: ``<venv>/bin/python``)."""

    return str(Path(sys.executable))


def python_is_symlinked() -> bool:
    """실행에 사용된 인터프리터가 다른 경로로 resolve되면 True."""

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
    """프레임워크가 답하면 ``True``/``False``, 확인할 수 없으면 ``None``."""

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
    """존재하는 저장소 루트 중 하나라도 목록을 읽을 수 있으면 True."""

    existing = [root for root in roots if root.is_dir()]
    if not existing:
        return False
    return any(path_readable(root) for root in existing)


def collect(settings: Settings, store_roots: Iterable[Path]) -> dict[str, Any]:
    """첫 실행 안내와 ``status``에 필요한 정보. 개인정보는 담지 않는다."""

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
    """빠진 권한에 대해 사람이 읽고 따라 할 다음 단계를 만든다."""

    exe = report.get("python_executable", "?")
    lines: list[str] = []
    if report.get("python_is_symlinked"):
        lines.append(
            "이 인터프리터는 심볼릭 링크입니다. macOS는 권한을 resolve된 바이너리에 기록합니다.\n"
            f"  실행에 사용된 경로: {report.get('invoked_executable', '?')}\n"
            f"  권한 부여 대상: {exe}\n"
            "  해당 Python 빌드를 업그레이드하면 권한이 무효가 됩니다 — 버전을 바꿨다면 venv를 다시 "
            "만들고 권한을 다시 부여하세요."
        )
    if not report.get("full_disk_access_ok"):
        lines.append(
            "카카오톡 DB를 읽으려면 전체 디스크 접근 권한이 필요합니다.\n"
            f"  이 실행 파일에 부여하세요: {exe}\n"
            "  시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한\n"
            "  (`sesametok-backend permissions`로 해당 설정 창을 엽니다)"
        )
    if report.get("accessibility_trusted") is False:
        lines.append(
            "카카오톡 UI로 메시지를 보내려면 손쉬운 사용 권한이 필요합니다.\n"
            f"  이 실행 파일에 부여하세요: {exe}\n"
            "  시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용\n"
            "  (`sesametok-backend permissions --accessibility`)"
        )
    if not report.get("kakaotalk_installed"):
        lines.append("KakaoTalk.app을 /Applications에서 찾을 수 없습니다.")
    elif not report.get("kakaotalk_running"):
        lines.append("카카오톡이 설치되어 있지만 실행 중이 아닙니다. 새 메시지가 나타나지 않습니다.")
    return lines
