from __future__ import annotations

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from sesametok_backend.kakao import cipher
from sesametok_backend.kakao.derive import secure_key
from sesametok_backend.kakao.errors import KakaoCipherError, KakaoWrongKeyError
from support import (
    SYNTHETIC_UUID,
    SYNTHETIC_USER_ID,
    write_synthetic_database,
    write_synthetic_store,
)

WRONG_UUID = "00000000-1111-2222-3333-555555555555"


def test_correct_key_opens_and_reads(tmp_path) -> None:
    path = write_synthetic_database(tmp_path / "synthetic.db")

    connection = cipher.connect_readonly(path, secure_key(SYNTHETIC_USER_ID, SYNTHETIC_UUID))
    try:
        assert "NTChatRoom" in cipher.table_names(connection)
        assert cipher.table_columns(connection, "NTChatMessage") >= {"chatId", "logId"}
        assert (cipher.cipher_version(connection) or "").startswith("4")
    finally:
        connection.close()


def test_wrong_key_is_rejected(tmp_path) -> None:
    path = write_synthetic_database(tmp_path / "synthetic.db")

    with pytest.raises(KakaoWrongKeyError):
        cipher.connect_readonly(path, "ff" * 128)


def test_short_key_is_rejected_before_touching_the_database(tmp_path) -> None:
    path = write_synthetic_database(tmp_path / "synthetic.db")

    with pytest.raises(KakaoCipherError):
        cipher.connect_readonly(path, "ff" * 32)


def test_connection_is_read_only(tmp_path) -> None:
    path = write_synthetic_database(tmp_path / "synthetic.db")
    connection = cipher.connect_readonly(path, secure_key(SYNTHETIC_USER_ID, SYNTHETIC_UUID))
    try:
        with pytest.raises(sqlcipher.Error):
            connection.execute("CREATE TABLE should_not_exist (a INTEGER)")
    finally:
        connection.close()


def test_unknown_table_has_no_columns(tmp_path) -> None:
    path = write_synthetic_database(tmp_path / "synthetic.db")
    connection = cipher.connect_readonly(path, secure_key(SYNTHETIC_USER_ID, SYNTHETIC_UUID))
    try:
        assert cipher.table_columns(connection, "Nope") == set()
    finally:
        connection.close()


def test_probe_finds_the_account_database(tmp_path) -> None:
    write_synthetic_store(tmp_path)

    found = cipher.probe(SYNTHETIC_USER_ID, SYNTHETIC_UUID, (tmp_path,))
    missing = cipher.probe(SYNTHETIC_USER_ID, WRONG_UUID, (tmp_path,))

    assert found is not None
    assert missing is None


def test_probe_returns_none_for_a_wrong_user_id(tmp_path) -> None:
    write_synthetic_store(tmp_path)

    assert cipher.probe(SYNTHETIC_USER_ID + 1, SYNTHETIC_UUID, (tmp_path,)) is None
