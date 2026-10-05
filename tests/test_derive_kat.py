"""Known-answer tests for the KakaoTalk key derivation.

These vectors are derived from fully synthetic inputs; they contain no real
account data. If they fail, the implementation is wrong — never update them to
match the code.
"""

from __future__ import annotations

import pytest

from sesametok_backend.kakao.derive import (
    ACCOUNT_HASH_LENGTH,
    DATABASE_NAME_LENGTH,
    KAT_DATABASE_NAME,
    KAT_HASHED_DEVICE_UUID,
    KAT_SECURE_KEY,
    KAT_USER_ID,
    KAT_UUID,
    SECURE_KEY_LENGTH,
    KeyDerivationError,
    account_hash,
    database_name,
    hashed_device_uuid,
    secure_key,
    validate_uuid,
    verify_lengths,
)


def test_hashed_device_uuid_matches_vector() -> None:
    assert hashed_device_uuid(KAT_UUID) == KAT_HASHED_DEVICE_UUID


def test_database_name_matches_vector() -> None:
    assert database_name(KAT_USER_ID, KAT_UUID) == KAT_DATABASE_NAME


def test_secure_key_matches_vector() -> None:
    assert secure_key(KAT_USER_ID, KAT_UUID) == KAT_SECURE_KEY


def test_vector_lengths() -> None:
    assert len(KAT_DATABASE_NAME) == DATABASE_NAME_LENGTH == 78
    assert len(KAT_SECURE_KEY) == SECURE_KEY_LENGTH == 256
    assert len(account_hash(KAT_USER_ID)) == ACCOUNT_HASH_LENGTH == 64


def test_derivations_are_deterministic() -> None:
    assert database_name(KAT_USER_ID, KAT_UUID) == database_name(KAT_USER_ID, KAT_UUID)
    assert secure_key(KAT_USER_ID, KAT_UUID) == secure_key(KAT_USER_ID, KAT_UUID)


def test_derivations_depend_on_both_inputs() -> None:
    other_uuid = "00000000-1111-2222-3333-555555555555"
    assert database_name(KAT_USER_ID, other_uuid) != KAT_DATABASE_NAME
    assert secure_key(KAT_USER_ID, other_uuid) != KAT_SECURE_KEY
    assert database_name(KAT_USER_ID + 1, KAT_UUID) != KAT_DATABASE_NAME
    assert secure_key(KAT_USER_ID + 1, KAT_UUID) != KAT_SECURE_KEY


def test_account_hash_is_sha256_of_decimal_user_id() -> None:
    import hashlib

    assert account_hash(42) == hashlib.sha256(b"42").hexdigest()


def test_secure_key_is_lowercase_hex() -> None:
    key = secure_key(KAT_USER_ID, KAT_UUID)
    assert key == key.lower()
    assert set(key) <= set("0123456789abcdef")


def test_short_uuid_is_rejected() -> None:
    with pytest.raises(KeyDerivationError):
        hashed_device_uuid("too-short")
    with pytest.raises(KeyDerivationError):
        database_name(KAT_USER_ID, "too-short")
    with pytest.raises(KeyDerivationError):
        secure_key(KAT_USER_ID, "too-short")


def test_validate_uuid_accepts_exact_length() -> None:
    assert validate_uuid(KAT_UUID) == KAT_UUID


def test_verify_lengths_passes_for_any_positive_user_id() -> None:
    verify_lengths(1, KAT_UUID)
