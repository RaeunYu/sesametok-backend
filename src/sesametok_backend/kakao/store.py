"""KakaoTalk local storage discovery.

The desktop app keeps its data in one of two roots. Both are scanned; when the
same file name appears in both, the copy with the newest *effective* modification
time wins. A non-empty ``-wal`` counts as a modification even when the main file's
mtime is unchanged, because committed frames can sit in the WAL. An empty WAL and
``-shm`` are not freshness signals.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .derive import database_name
from .errors import KakaoDatabaseNotFound, KakaoStoreError

CONTAINER_ROOT = (
    Path.home()
    / "Library"
    / "Containers"
    / "com.kakao.KakaoTalkMac"
    / "Data"
    / "Library"
    / "Application Support"
    / "com.kakao.KakaoTalkMac"
)

UNSANDBOXED_ROOT = (
    Path.home() / "Library" / "Application Support" / "com.kakao.KakaoTalkMac"
)

PREFERENCES_DIRS = (
    Path.home()
    / "Library"
    / "Containers"
    / "com.kakao.KakaoTalkMac"
    / "Data"
    / "Library"
    / "Preferences",
    Path.home() / "Library" / "Preferences",
)

PREFERENCES_PREFIX = "com.kakao.KakaoTalkMac"

# 78 lowercase hex chars, optionally with a ``.db`` suffix. ``-wal``/``-shm`` are
# excluded because the suffix does not match.
DB_FILE_RE = re.compile(r"^[0-9a-f]{78}(?:\.db)?$")

DEVICE_UUID_LENGTH = 36


def store_roots() -> tuple[Path, ...]:
    """Both candidate storage roots, unsandboxed first."""

    return (UNSANDBOXED_ROOT, CONTAINER_ROOT)


@dataclass(frozen=True)
class DatabaseFile:
    """One discovered KakaoTalk database and its WAL state."""

    name: str
    path: Path
    size: int
    mtime: float
    wal_path: Path | None = None
    wal_size: int = 0
    wal_mtime: float | None = None

    @property
    def effective_mtime(self) -> float:
        """Newest signal: main file mtime, or a non-empty WAL's mtime."""

        if self.wal_path is not None and self.wal_size > 0 and self.wal_mtime is not None:
            return max(self.mtime, self.wal_mtime)
        return self.mtime

    @property
    def has_pending_wal(self) -> bool:
        return self.wal_path is not None and self.wal_size > 0


def _stat(path: Path) -> tuple[int, float] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime


def inspect_database(path: Path) -> DatabaseFile:
    """Stat *path* and its ``-wal`` sibling."""

    stat = _stat(path)
    if stat is None:
        raise KakaoStoreError(f"database is not readable: {path.name}")
    size, mtime = stat

    wal_path = path.with_name(path.name + "-wal")
    wal_stat = _stat(wal_path)
    wal_size, wal_mtime = (0, None) if wal_stat is None else wal_stat

    return DatabaseFile(
        name=path.name,
        path=path,
        size=size,
        mtime=mtime,
        wal_path=wal_path if wal_stat is not None else None,
        wal_size=wal_size,
        wal_mtime=wal_mtime,
    )


def discover_databases(roots: tuple[Path, ...] | None = None) -> tuple[DatabaseFile, ...]:
    """All distinct database file names across *roots*, newest copy per name."""

    best: dict[str, DatabaseFile] = {}
    for root in roots if roots is not None else store_roots():
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or not DB_FILE_RE.match(entry.name):
                continue
            candidate = inspect_database(Path(entry.path))
            current = best.get(candidate.name)
            if current is None or candidate.effective_mtime > current.effective_mtime:
                best[candidate.name] = candidate
    return tuple(best[name] for name in sorted(best))


def candidate_names(user_id: int, uuid: str) -> tuple[str, ...]:
    """The derived file name, with and without the optional ``.db`` suffix."""

    base = database_name(user_id, uuid)
    return (base, f"{base}.db")


def find_database(
    user_id: int,
    uuid: str,
    roots: tuple[Path, ...] | None = None,
) -> DatabaseFile | None:
    """Newest database whose name matches the derived name for this account."""

    wanted = set(candidate_names(user_id, uuid))
    matches = [db for db in discover_databases(roots) if db.name in wanted]
    if not matches:
        return None
    return max(matches, key=lambda db: db.effective_mtime)


def require_database(
    user_id: int,
    uuid: str,
    roots: tuple[Path, ...] | None = None,
) -> DatabaseFile:
    found = find_database(user_id, uuid, roots)
    if found is None:
        raise KakaoDatabaseNotFound(
            f"no KakaoTalk database matching the derived name for user_id={user_id}"
        )
    return found


def device_uuid() -> str:
    """The Mac's ``IOPlatformUUID`` (36 chars) via ``ioreg``."""

    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KakaoStoreError(f"could not run ioreg: {exc}") from exc

    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
    if match is None:
        raise KakaoStoreError("IORegistry did not expose IOPlatformUUID")
    uuid = match.group(1)
    if len(uuid) != DEVICE_UUID_LENGTH:
        raise KakaoStoreError(f"IOPlatformUUID is {len(uuid)} chars, expected {DEVICE_UUID_LENGTH}")
    return uuid
