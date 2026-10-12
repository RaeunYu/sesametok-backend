"""암호화된 로컬 아카이브 키: 환경변수 우선, 그다음 로그인 키체인.

ADR-004가 정한 사항이다. SQLCipher 아카이브 키는 macOS 로그인 키체인에 저장된
랜덤 32바이트 값이다. 테스트와 헤드리스 실행이 키체인을 건드리지 않고 동작하도록
``SESAMETOK_ARCHIVE_KEY`` (16진수 64자)가 우선한다.

키 파일 폴백과 평문 폴백은 의도적으로 없다. 키를 불러오거나 저장할 수 없으면
암호화되지 않은 아카이브를 조용히 기록하는 대신 명확한 오류와 함께 시작에
실패한다.
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from ..config import Settings

KEYCHAIN_SERVICE = "com.sesametok.sesametok-backend"
KEYCHAIN_ACCOUNT = "archive"
KEY_BYTES = 32
KEY_HEX_LENGTH = KEY_BYTES * 2
_HEX = re.compile(rf"^[0-9a-fA-F]{{{KEY_HEX_LENGTH}}}$")

KEY_BACKUP_HELP = (
    "출력된 키를 안전한 곳(비밀번호 관리자)에 보관하라. "
    "키를 잃으면 아카이브를 복호화할 수 없어 카카오톡에서 다시 만들어야 하며, "
    "그 밖의 방법으로는 복구할 수 없다."
)


class ArchiveKeyError(RuntimeError):
    """아카이브 키를 확인하거나 저장할 수 없었다."""


def parse_hex_key(value: str) -> bytes:
    """16진수 64자를 받는다. 선택적으로 ``x'...'`` 로 감싼 형태도 허용한다."""

    text = value.strip()
    if text.startswith("x'") and text.endswith("'"):
        text = text[2:-1]
    if not _HEX.match(text):
        raise ArchiveKeyError(
            f"아카이브 키는 16진수 {KEY_HEX_LENGTH}자({KEY_BYTES}바이트)여야 한다"
        )
    return bytes.fromhex(text)


def key_to_hex(key: bytes) -> str:
    if len(key) != KEY_BYTES:
        raise ArchiveKeyError(f"아카이브 키는 {KEY_BYTES}바이트여야 하는데 {len(key)}바이트다")
    return key.hex()


def pragma_literal(key: bytes) -> str:
    """SQLCipher 로우 키 리터럴: ``x'<hex>'``."""

    return f"x'{key_to_hex(key)}'"


def generate_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


class Keychain(Protocol):
    def get(self, service: str, account: str) -> bytes | None: ...

    def set(self, service: str, account: str, key: bytes) -> None: ...


class SecurityCliKeychain:
    """``/usr/bin/security`` 를 통한 키체인 접근."""

    def __init__(self, executable: str = "security") -> None:
        self.executable = executable

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if shutil.which(self.executable) is None:
            raise ArchiveKeyError("macOS `security` 도구를 사용할 수 없다")
        try:
            return subprocess.run(
                [self.executable, *args], capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ArchiveKeyError(f"`security`를 실행할 수 없다: {exc}") from exc

    def get(self, service: str, account: str) -> bytes | None:
        result = self._run(
            ["find-generic-password", "-w", "-s", service, "-a", account]
        )
        if result.returncode == 44:  # errSecItemNotFound
            return None
        if result.returncode != 0:
            raise ArchiveKeyError(
                f"키체인에서 아카이브 키를 읽을 수 없다: {result.stderr.strip()}"
            )
        return parse_hex_key(result.stdout)

    def set(self, service: str, account: str, key: bytes) -> None:
        result = self._run(
            ["add-generic-password", "-U", "-s", service, "-a", account, "-w", key_to_hex(key)]
        )
        if result.returncode != 0:
            raise ArchiveKeyError(
                "키체인에 아카이브 키를 저장할 수 없다: "
                f"{result.stderr.strip()}"
            )


@dataclass(frozen=True)
class ArchiveKey:
    key: bytes
    source: str  # "env" | "keychain" | "created"

    @property
    def is_new(self) -> bool:
        return self.source == "created"


def resolve_archive_key(
    settings: Settings,
    *,
    keychain: Keychain | None = None,
    service: str = KEYCHAIN_SERVICE,
    account: str = KEYCHAIN_ACCOUNT,
) -> ArchiveKey:
    """환경변수 우선, 다음 키체인, 마지막으로 생성 후 저장."""

    if settings.archive_key_hex:
        return ArchiveKey(key=parse_hex_key(settings.archive_key_hex), source="env")

    backend = keychain if keychain is not None else SecurityCliKeychain()
    existing = backend.get(service, account)
    if existing is not None:
        return ArchiveKey(key=existing, source="keychain")

    key = generate_key()
    backend.set(service, account, key)
    return ArchiveKey(key=key, source="created")
