"""Opening the KakaoTalk database read-only with SQLCipher.

Two rules are encoded here and must not drift:

1. The 256-hex ``secure_key`` is passed as a **passphrase string**, not as a raw
   key literal: ``PRAGMA key = '<256 hex>'``. SQLCipher then runs its own KDF
   over that passphrase and the database salt. Using ``x'<hex>'`` here would hand
   SQLCipher 128 bytes of "raw key" instead and the database would reject it.
2. ``PRAGMA key`` first, ``PRAGMA cipher_compatibility = 3`` second. Reversing
   them resets the cipher state and produces ``file is not a database``.

The original database is never modified. Connections are opened with ``mode=ro``
and ``query_only``.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from sqlcipher3 import dbapi2 as sqlcipher

from .derive import SECURE_KEY_LENGTH, secure_key
from .errors import KakaoCipherError, KakaoWrongKeyError
from .store import DatabaseFile, find_database

CIPHER_COMPATIBILITY = 3
WRONG_KEY_MARKERS = ("not a database", "file is encrypted")
_SECURE_KEY_RE = re.compile(rf"^[0-9a-f]{{{SECURE_KEY_LENGTH}}}$")


def _validate_passphrase(secure_key_hex: str) -> str:
    if not _SECURE_KEY_RE.match(secure_key_hex):
        raise KakaoCipherError(
            f"secure_key must be {SECURE_KEY_LENGTH} lowercase hex characters, "
            f"got {len(secure_key_hex)}"
        )
    return secure_key_hex

CIPHER_COMPATIBILITY = 3
WRONG_KEY_MARKERS = ("not a database", "file is encrypted")


def _is_wrong_key(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in WRONG_KEY_MARKERS)


def connect_readonly(
    path: Path,
    secure_key_hex: str,
    *,
    timeout: float = 5.0,
    compatibility: int = CIPHER_COMPATIBILITY,
):
    """Open *path* read-only and prove the key works. Raises on failure."""

    passphrase = _validate_passphrase(secure_key_hex)
    uri = f"file:{quote(str(path))}?mode=ro"
    try:
        connection = sqlcipher.connect(uri, uri=True, timeout=timeout)
    except sqlcipher.Error as exc:  # pragma: no cover - depends on local state
        raise KakaoCipherError(f"could not open the database: {exc}") from exc

    try:
        # Passphrase, not a raw key literal — the 256-hex string is the secret.
        connection.execute(f"PRAGMA key = '{passphrase}'")
        connection.execute(f"PRAGMA cipher_compatibility = {compatibility}")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher.Error as exc:
        connection.close()
        message = str(exc)
        if _is_wrong_key(message):
            raise KakaoWrongKeyError("the derived key was rejected by the database") from exc
        raise KakaoCipherError(f"could not unlock the database: {message}") from exc

    try:
        connection.execute("PRAGMA query_only = 1")
    except sqlcipher.Error:  # pragma: no cover - older builds without query_only
        pass
    return connection


def connect_for_identity(user_id: int, uuid: str, roots=None):
    """Open the newest database for *user_id*/*uuid* or raise."""

    database = find_database(user_id, uuid, roots)
    if database is None:
        from .errors import KakaoDatabaseNotFound

        raise KakaoDatabaseNotFound("no database file matched the derived name")
    return connect_readonly(database.path, secure_key(user_id, uuid))


def probe(user_id: int, uuid: str, roots=None) -> DatabaseFile | None:
    """Try to unlock the account's database. Returns the file when it works."""

    database = find_database(user_id, uuid, roots)
    if database is None:
        return None
    try:
        connection = connect_readonly(database.path, secure_key(user_id, uuid))
    except KakaoWrongKeyError:
        return None
    except KakaoCipherError:
        return None
    connection.close()
    return database


def cipher_version(connection) -> str | None:
    try:
        row = connection.execute("PRAGMA cipher_version;").fetchone()
    except sqlcipher.Error:
        return None
    return None if not row else str(row[0])


def table_names(connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {str(row[0]) for row in rows}


def table_columns(connection, table: str) -> set[str]:
    """Column names of *table*, or an empty set when it cannot be inspected."""

    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlcipher.Error:
        return set()
    return {str(row[1]) for row in rows}
