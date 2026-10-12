"""SQLCipher로 카카오톡 데이터베이스를 읽기 전용으로 연다.

여기에는 반드시 지켜야 할 두 가지 규칙이 담겨 있다.

1. 256-hex ``secure_key`` 는 로우 키 리터럴이 아니라 **패스프레이즈 문자열**로
   전달한다: ``PRAGMA key = '<256 hex>'``. 그러면 SQLCipher가 그 패스프레이즈와
   데이터베이스 솔트로 자체 KDF를 수행한다. 여기서 ``x'<hex>'`` 를 쓰면
   SQLCipher에 128바이트 "로우 키"를 대신 넘기게 되고 데이터베이스가 이를
   거부한다.
2. ``PRAGMA key`` 를 먼저, ``PRAGMA cipher_compatibility = 3`` 을 나중에
   실행한다. 순서를 뒤집으면 암호 상태가 초기화되어
   ``file is not a database`` 가 발생한다.

원본 데이터베이스는 절대 수정하지 않는다. 연결은 ``mode=ro`` 와 ``query_only``
로 연다.
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
            f"secure_key는 소문자 16진수 {SECURE_KEY_LENGTH}자여야 하는데 "
            f"{len(secure_key_hex)}자다"
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
    """*path* 를 읽기 전용으로 열고 키가 동작함을 확인한다. 실패하면 예외를 발생시킨다."""

    passphrase = _validate_passphrase(secure_key_hex)
    uri = f"file:{quote(str(path))}?mode=ro"
    try:
        connection = sqlcipher.connect(uri, uri=True, timeout=timeout)
    except sqlcipher.Error as exc:  # pragma: no cover - 로컬 상태에 따라 달라짐
        raise KakaoCipherError(f"데이터베이스를 열 수 없다: {exc}") from exc

    try:
        # 로우 키 리터럴이 아니라 패스프레이즈 — 256-hex 문자열이 비밀값이다.
        connection.execute(f"PRAGMA key = '{passphrase}'")
        connection.execute(f"PRAGMA cipher_compatibility = {compatibility}")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher.Error as exc:
        connection.close()
        message = str(exc)
        if _is_wrong_key(message):
            raise KakaoWrongKeyError("유도된 키가 데이터베이스에서 거부되었다") from exc
        raise KakaoCipherError(f"데이터베이스 잠금을 해제할 수 없다: {message}") from exc

    try:
        connection.execute("PRAGMA query_only = 1")
    except sqlcipher.Error:  # pragma: no cover - query_only가 없는 구버전 빌드
        pass
    return connection


def connect_for_identity(user_id: int, uuid: str, roots=None):
    """*user_id*/*uuid* 의 가장 최신 데이터베이스를 열고, 없으면 예외를 발생시킨다."""

    database = find_database(user_id, uuid, roots)
    if database is None:
        from .errors import KakaoDatabaseNotFound

        raise KakaoDatabaseNotFound("유도된 이름과 일치하는 데이터베이스 파일이 없다")
    return connect_readonly(database.path, secure_key(user_id, uuid))


def probe(user_id: int, uuid: str, roots=None) -> DatabaseFile | None:
    """해당 계정의 데이터베이스 잠금 해제를 시도한다. 성공하면 그 파일을 반환한다."""

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
    """*table* 의 컬럼 이름들. 조회할 수 없으면 빈 집합."""

    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlcipher.Error:
        return set()
    return {str(row[1]) for row in rows}
