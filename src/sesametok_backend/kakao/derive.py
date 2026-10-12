"""카카오톡 키 유도 — 정확한 알고리즘.

PBKDF2-HMAC-SHA256, 100,000 라운드, 128바이트 출력, 소문자 16진수로 표현한다.

이 코드를 "정리"하지 마라. 모든 단계(``F`` 결합, 문자열 뒤집기, 소수 인덱스
솔트 슬라이스, ``[28:106]`` 윈도)가 핵심을 지탱한다. 유도된 이름과
패스프레이즈는 카카오톡 앱이 계산하는 값과 일치해야 하며, :data:`KAT_*` 의
합성 known-answer 벡터가 이를 고정한다.
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
    """입력을 유도에 사용할 수 없을 때 발생한다."""


def validate_uuid(uuid: str) -> str:
    """36자 기기 UUID 형식을 검증하고 그대로 반환한다."""

    if len(uuid) != UUID_LENGTH:
        raise KeyDerivationError(f"기기 UUID는 {UUID_LENGTH}자여야 하는데 {len(uuid)}자다")
    return uuid


def hashed_device_uuid(uuid: str) -> str:
    """``base64(sha1(uuid) || sha256(uuid))`` — 56자 ASCII 문자열."""

    validate_uuid(uuid)
    raw = uuid.encode("ascii")
    combined = hashlib.sha1(raw).digest() + hashlib.sha256(raw).digest()  # 20 + 32 바이트
    return base64.b64encode(combined).decode("ascii")


def _pbkdf2(password: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, PBKDF2_ROUNDS, dklen=PBKDF2_DKLEN)


def secure_key(user_id: int, uuid: str) -> str:
    """카카오톡 데이터베이스용 SQLCipher 패스프레이즈: 소문자 16진수 256자."""

    validate_uuid(uuid)
    hashed = hashed_device_uuid(uuid)
    parts = ["A", hashed, "|", "F", uuid[0:5], "H", str(user_id), "|", uuid[7:]]
    joined = "F".join(parts)
    hawawa = joined[::-1]
    salt = uuid[int(len(uuid) * 0.3) :]  # 36자 -> 인덱스 10
    return _pbkdf2(hawawa.encode("ascii"), salt.encode("ascii")).hex()


def database_name(user_id: int, uuid: str) -> str:
    """디스크상의 데이터베이스 파일 이름: 소문자 16진수 78자, 확장자 없음."""

    validate_uuid(uuid)
    parts = [".", "F", str(user_id), "A", "F", uuid[::-1], ".", "|"]
    hawawa = ".".join(parts)
    salt = hashed_device_uuid(uuid)[::-1]
    return _pbkdf2(hawawa.encode("ascii"), salt.encode("ascii")).hex()[28 : 28 + 78]


def account_hash(user_id: int) -> str:
    """안정적인 아카이브 파티션 키: ``sha256(str(user_id))``, 16진수 64자."""

    return hashlib.sha256(str(user_id).encode("ascii")).hexdigest()


def verify_lengths(user_id: int, uuid: str) -> None:
    """알고리즘이 여전히 올바른 형식의 출력을 내는지 확인하는 저렴한 자체 점검."""

    name = database_name(user_id, uuid)
    key = secure_key(user_id, uuid)
    if len(name) != DATABASE_NAME_LENGTH:
        raise KeyDerivationError(f"database_name 길이 {len(name)} != {DATABASE_NAME_LENGTH}")
    if len(key) != SECURE_KEY_LENGTH:
        raise KeyDerivationError(f"secure_key 길이 {len(key)} != {SECURE_KEY_LENGTH}")


# --------------------------------------------------------------------------- KAT
# 전부 합성 입력이다. 실제 계정과는 무관하며, 리팩터가 유도를 조용히 바꾸지
# 못하도록 하기 위해서만 존재한다.

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
