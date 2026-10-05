"""Encrypted local archive key: environment override, else the login keychain.

ADR-004 decides this: the SQLCipher archive key is a random 32-byte value stored
in the macOS login keychain. ``SESAMETOK_ARCHIVE_KEY`` (64 hex chars) takes
precedence so tests and headless runs work without touching the keychain.

There is deliberately no key-file fallback and no plaintext fallback: if the key
cannot be loaded or stored, startup fails with a clear error instead of silently
writing an unencrypted archive.
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
    "Store the printed key somewhere safe (a password manager). "
    "If it is lost the archive cannot be decrypted and has to be rebuilt from "
    "KakaoTalk; nothing else can recover it."
)


class ArchiveKeyError(RuntimeError):
    """The archive key could not be resolved or persisted."""


def parse_hex_key(value: str) -> bytes:
    """Accept 64 hex chars, optionally wrapped as ``x'...'``."""

    text = value.strip()
    if text.startswith("x'") and text.endswith("'"):
        text = text[2:-1]
    if not _HEX.match(text):
        raise ArchiveKeyError(
            f"archive key must be {KEY_HEX_LENGTH} hex characters ({KEY_BYTES} bytes)"
        )
    return bytes.fromhex(text)


def key_to_hex(key: bytes) -> str:
    if len(key) != KEY_BYTES:
        raise ArchiveKeyError(f"archive key must be {KEY_BYTES} bytes, got {len(key)}")
    return key.hex()


def pragma_literal(key: bytes) -> str:
    """SQLCipher raw-key literal: ``x'<hex>'``."""

    return f"x'{key_to_hex(key)}'"


def generate_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


class Keychain(Protocol):
    def get(self, service: str, account: str) -> bytes | None: ...

    def set(self, service: str, account: str, key: bytes) -> None: ...


class SecurityCliKeychain:
    """Keychain access through ``/usr/bin/security``."""

    def __init__(self, executable: str = "security") -> None:
        self.executable = executable

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if shutil.which(self.executable) is None:
            raise ArchiveKeyError("the macOS `security` tool is not available")
        try:
            return subprocess.run(
                [self.executable, *args], capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ArchiveKeyError(f"could not run `security`: {exc}") from exc

    def get(self, service: str, account: str) -> bytes | None:
        result = self._run(
            ["find-generic-password", "-w", "-s", service, "-a", account]
        )
        if result.returncode == 44:  # errSecItemNotFound
            return None
        if result.returncode != 0:
            raise ArchiveKeyError(
                f"could not read the archive key from the keychain: {result.stderr.strip()}"
            )
        return parse_hex_key(result.stdout)

    def set(self, service: str, account: str, key: bytes) -> None:
        result = self._run(
            ["add-generic-password", "-U", "-s", service, "-a", account, "-w", key_to_hex(key)]
        )
        if result.returncode != 0:
            raise ArchiveKeyError(
                "could not store the archive key in the keychain: "
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
    """Environment first, then keychain, then generate-and-store."""

    if settings.archive_key_hex:
        return ArchiveKey(key=parse_hex_key(settings.archive_key_hex), source="env")

    backend = keychain if keychain is not None else SecurityCliKeychain()
    existing = backend.get(service, account)
    if existing is not None:
        return ArchiveKey(key=existing, source="keychain")

    key = generate_key()
    backend.set(service, account, key)
    return ArchiveKey(key=key, source="created")
