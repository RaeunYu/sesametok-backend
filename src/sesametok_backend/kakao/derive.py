"""KakaoTalk key derivation — the exact algorithm.

PBKDF2-HMAC-SHA256, 100,000 rounds, 128-byte output, rendered as lowercase hex.

Do not "clean up" this code. Every step (the ``F`` joins, the reversed string,
the fractional-index salt slice, the ``[28:106]`` window) is load-bearing: the
derived names and passphrases must match what the KakaoTalk app computes, and the
synthetic known-answer vectors in :data:`KAT_*` lock that in.
"""

from __future__ import annotations

import base64
import hashlib

PBKDF2_ROUNDS = 100_000
PBKDF2_DKLEN = 128

UUID_LENGTH = 36
DATABASE_NAME_LENGTH = 78
SECURE_KEY_LENGTH = 256
ACCOUNT_HASH_LENGTH = 64


class KeyDerivationError(ValueError):
    """Raised when an input cannot be used for derivation."""


def validate_uuid(uuid: str) -> str:
    """Validate the 36-character device UUID shape and return it unchanged."""

    if len(uuid) != UUID_LENGTH:
        raise KeyDerivationError(f"device UUID must be {UUID_LENGTH} characters, got {len(uuid)}")
    return uuid


def hashed_device_uuid(uuid: str) -> str:
    """``base64(sha1(uuid) || sha256(uuid))`` — a 56-character ASCII string."""

    validate_uuid(uuid)
    raw = uuid.encode("ascii")
    combined = hashlib.sha1(raw).digest() + hashlib.sha256(raw).digest()  # 20 + 32 bytes
    return base64.b64encode(combined).decode("ascii")


def _pbkdf2(password: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, PBKDF2_ROUNDS, dklen=PBKDF2_DKLEN)


def secure_key(user_id: int, uuid: str) -> str:
    """SQLCipher passphrase for the KakaoTalk database: 256 lowercase hex chars."""

    validate_uuid(uuid)
    hashed = hashed_device_uuid(uuid)
    parts = ["A", hashed, "|", "F", uuid[0:5], "H", str(user_id), "|", uuid[7:]]
    joined = "F".join(parts)
    hawawa = joined[::-1]
    salt = uuid[int(len(uuid) * 0.3) :]  # 36 chars -> index 10
    return _pbkdf2(hawawa.encode("ascii"), salt.encode("ascii")).hex()


def database_name(user_id: int, uuid: str) -> str:
    """On-disk database file name: 78 lowercase hex chars, no extension."""

    validate_uuid(uuid)
    parts = [".", "F", str(user_id), "A", "F", uuid[::-1], ".", "|"]
    hawawa = ".".join(parts)
    salt = hashed_device_uuid(uuid)[::-1]
    return _pbkdf2(hawawa.encode("ascii"), salt.encode("ascii")).hex()[28 : 28 + 78]


def account_hash(user_id: int) -> str:
    """Stable archive partition key: ``sha256(str(user_id))``, 64 hex chars."""

    return hashlib.sha256(str(user_id).encode("ascii")).hexdigest()


def verify_lengths(user_id: int, uuid: str) -> None:
    """Cheap self-check that the algorithm still produces well-formed output."""

    name = database_name(user_id, uuid)
    key = secure_key(user_id, uuid)
    if len(name) != DATABASE_NAME_LENGTH:
        raise KeyDerivationError(f"database_name length {len(name)} != {DATABASE_NAME_LENGTH}")
    if len(key) != SECURE_KEY_LENGTH:
        raise KeyDerivationError(f"secure_key length {len(key)} != {SECURE_KEY_LENGTH}")


# --------------------------------------------------------------------------- KAT
# Fully synthetic inputs. They are unrelated to any real account; they exist only
# so a refactor cannot silently change the derivation.

KAT_UUID = "00000000-1111-2222-3333-444444444444"
KAT_USER_ID = 1000000001
KAT_HASHED_DEVICE_UUID = (
    "rFTdocjblYTf9KMgqtef5/dp9tDRb+Mi/VuO1qVxVuhEAa7bJRKXlB8/M5QIvHgYaQOo7Q=="
)
KAT_DATABASE_NAME = (
    "de345a8eb68ff0db3c1f8b94817936a00471d335162afc05cdfc758f638a33d427ea7742d4d420"
)
KAT_SECURE_KEY = (
    "30231c3546eada7fee9584912ce5fb779cf9699b3267c6910f390039ad5f0ba9a144fc24a28375ee"
    "22c12396fc713f0b0b85a6b337d203ed64177eef9dbfbc7d379920e8ba47ee805a1a2f5bd8e11dcd"
    "3680fa1375972596981725646f06f61883a4bd816b3faaa062d0222707bbd4bfb6631f52ef510dca"
    "97308f116690e7a0"
)
