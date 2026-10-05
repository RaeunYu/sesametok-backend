"""Filesystem helpers with the private-permission and atomic-write rules.

The project owns local state (the encrypted archive, the API token, the KakaoTalk
auth cache, the outbox). Every writer in this package goes through here so that
permissions are consistently ``0700`` for directories and ``0600`` for files, and
secrets never land in a partially written file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def ensure_dir(path: Path) -> Path:
    """Create *path* (and parents) if missing. Permissions are left alone."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_private_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """Create *path* and force it to *mode*, tolerating filesystems that cannot chmod."""

    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def atomic_write_bytes(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """Write *data* to *path* atomically with *mode* (temp file + ``os.replace``)."""

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
    """Return the permission bits of *path*, or ``None`` when it cannot be read."""

    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


def tighten(path: Path, mode: int = FILE_MODE) -> bool:
    """Best-effort ``chmod``. Returns ``True`` when the mode was applied."""

    try:
        os.chmod(path, mode)
    except OSError:
        return False
    return True
