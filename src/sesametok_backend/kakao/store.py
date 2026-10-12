"""카카오톡 로컬 저장소 탐색.

데스크톱 앱은 데이터를 두 루트 중 하나에 보관한다. 두 곳을 모두 검사하며, 같은
파일 이름이 양쪽에 있으면 *실효* 수정 시각이 가장 최신인 사본이 이긴다. 본체
파일의 mtime이 그대로여도 비어 있지 않은 ``-wal`` 은 수정으로 간주하는데, 커밋된
프레임이 WAL에 남아 있을 수 있기 때문이다. 빈 WAL과 ``-shm`` 은 최신성 신호가
아니다.
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

# 소문자 16진수 78자, 선택적으로 ``.db`` 접미사가 붙는다. ``-wal``/``-shm`` 은
# 접미사가 일치하지 않으므로 제외된다.
DB_FILE_RE = re.compile(r"^[0-9a-f]{78}(?:\.db)?$")

DEVICE_UUID_LENGTH = 36


def store_roots() -> tuple[Path, ...]:
    """두 후보 저장소 루트. 샌드박스 밖 경로가 먼저다."""

    return (UNSANDBOXED_ROOT, CONTAINER_ROOT)


@dataclass(frozen=True)
class DatabaseFile:
    """발견된 카카오톡 데이터베이스 하나와 그 WAL 상태."""

    name: str
    path: Path
    size: int
    mtime: float
    wal_path: Path | None = None
    wal_size: int = 0
    wal_mtime: float | None = None

    @property
    def effective_mtime(self) -> float:
        """가장 최신 신호: 본체 파일 mtime 또는 비어 있지 않은 WAL의 mtime."""

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
    """*path* 와 그 ``-wal`` 형제 파일의 상태를 조회한다."""

    stat = _stat(path)
    if stat is None:
        raise KakaoStoreError(f"데이터베이스를 읽을 수 없다: {path.name}")
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
    """*roots* 전체에서 이름이 서로 다른 모든 데이터베이스 파일. 이름별로 가장 최신 사본."""

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
    """유도된 파일 이름. 선택적 ``.db`` 접미사가 붙은 것과 붙지 않은 것 모두."""

    base = database_name(user_id, uuid)
    return (base, f"{base}.db")


def find_database(
    user_id: int,
    uuid: str,
    roots: tuple[Path, ...] | None = None,
) -> DatabaseFile | None:
    """이 계정의 유도된 이름과 일치하는 가장 최신 데이터베이스."""

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
            f"user_id={user_id} 의 유도된 이름과 일치하는 카카오톡 데이터베이스가 없다"
        )
    return found


def device_uuid() -> str:
    """``ioreg`` 로 얻은 Mac의 ``IOPlatformUUID`` (36자)."""

    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KakaoStoreError(f"ioreg를 실행할 수 없다: {exc}") from exc

    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
    if match is None:
        raise KakaoStoreError("IORegistry가 IOPlatformUUID를 노출하지 않았다")
    uuid = match.group(1)
    if len(uuid) != DEVICE_UUID_LENGTH:
        raise KakaoStoreError(f"IOPlatformUUID가 {len(uuid)}자인데 {DEVICE_UUID_LENGTH}자를 기대했다")
    return uuid
