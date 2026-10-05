"""A resumable read source over the live KakaoTalk database.

The database file can be replaced while KakaoTalk runs, so every read re-resolves
which copy is newest and opens a fresh read-only connection. Connections are
cheap; a stale handle is the real risk.
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
    """Resolves the account lazily so the poller can own the failure/backoff loop.

    Startup still resolves the identity explicitly (an unresolvable account is a
    hard failure), but keeping this wrapper means a
    transient database lock during a later poll is retried, not fatal.
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
