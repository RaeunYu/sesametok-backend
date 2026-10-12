"""비공개 권한과 원자적 쓰기 규칙을 따르는 파일시스템 헬퍼.

이 프로젝트는 로컬 상태(암호화 아카이브, API 토큰, 카카오톡 인증 캐시, 아웃박스)를
소유한다. 이 패키지의 모든 기록자는 여기를 거치므로 권한이 디렉터리는 일관되게
``0700``, 파일은 ``0600``이고, 비밀 값이 부분적으로 기록된 파일에 남지 않는다.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def ensure_dir(path: Path) -> Path:
    """없으면 *path*(와 상위 디렉터리)를 만든다. 권한은 건드리지 않는다."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_private_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """*path*를 만들고 *mode*로 강제한다. chmod를 할 수 없는 파일시스템은 허용한다."""

    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def atomic_write_bytes(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """*data*를 *mode*로 *path*에 원자적으로 쓴다 (임시 파일 + ``os.replace``)."""

    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, mode: int = FILE_MODE) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def read_text(path: Path, default: str | None = None) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError:
        return default


def file_mode(path: Path) -> int | None:
    """*path*의 권한 비트를 반환하고, 읽을 수 없으면 ``None``을 반환한다."""

    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


def tighten(path: Path, mode: int = FILE_MODE) -> bool:
    """최선을 다해 ``chmod``한다. 모드가 적용되면 ``True``를 반환한다."""

    try:
        os.chmod(path, mode)
    except OSError:
        return False
    return True
