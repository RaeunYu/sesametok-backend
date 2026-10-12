"""실행 중인 카카오톡 데이터베이스 위의 재개 가능한 읽기 소스.

카카오톡이 실행되는 동안 데이터베이스 파일이 교체될 수 있으므로, 모든 읽기에서
어느 사본이 가장 최신인지 다시 확인하고 새 읽기 전용 연결을 연다. 연결은 저렴하고,
진짜 위험은 오래된 핸들이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from . import cipher, store
from .auth import AccountIdentity, resolve_identity
from .derive import secure_key
from .reader import KakaoReader, Snapshot


@dataclass(frozen=True)
class KakaoSource:
    identity: AccountIdentity
    roots: tuple[Path, ...] | None = None

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        database = store.require_database(self.identity.user_id, self.identity.uuid, self.roots)
        connection = cipher.connect_readonly(
            database.path, secure_key(self.identity.user_id, self.identity.uuid)
        )
        try:
            reader = KakaoReader(connection, user_id=self.identity.user_id)
            return reader.snapshot(since_epoch=since_epoch)
        finally:
            connection.close()


@dataclass
class ResolvingKakaoSource:
    """계정을 지연 해석하여 폴러가 실패/백오프 루프를 담당할 수 있게 한다.

    시작 시에는 여전히 신원을 명시적으로 해석한다(해석할 수 없는 계정은
    치명적 실패다). 다만 이 래퍼를 유지함으로써 이후 폴링 중 발생하는 일시적인
    데이터베이스 잠금은 재시도 대상이 되고 치명적이지 않다.
    """

    settings: Settings
    roots: tuple[Path, ...] | None = None
    account_uuid: str | None = None
    allow_reverse_lookup: bool = True
    _resolved: KakaoSource | None = field(default=None, init=False)

    @property
    def identity(self) -> AccountIdentity | None:
        return None if self._resolved is None else self._resolved.identity

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        if self._resolved is None:
            identity = resolve_identity(
                self.settings,
                uuid_override=self.account_uuid,
                roots=self.roots,
                allow_reverse_lookup=self.allow_reverse_lookup,
            )
            self._resolved = KakaoSource(identity=identity, roots=self.roots)
        return self._resolved.read_snapshot(since_epoch=since_epoch)
