from __future__ import annotations

from pathlib import Path

import pytest

from sesametok_backend.config import Settings
from sesametok_backend.security.archive_key import (
    KEY_BYTES,
    ArchiveKeyError,
    SecurityCliKeychain,
    generate_key,
    key_to_hex,
    parse_hex_key,
    pragma_literal,
    resolve_archive_key,
)

HEX_KEY = "ab" * 32


class FakeKeychain:
    def __init__(self, stored: bytes | None = None, *, fail_set: bool = False) -> None:
        self.stored = stored
        self.fail_set = fail_set
        self.set_calls: list[tuple[str, str, bytes]] = []

    def get(self, service: str, account: str) -> bytes | None:
        return self.stored

    def set(self, service: str, account: str, key: bytes) -> None:
        if self.fail_set:
            raise ArchiveKeyError("keychain write refused")
        self.set_calls.append((service, account, key))
        self.stored = key


def _settings(tmp_path: Path, **env: str) -> Settings:
    base = {"SESAMETOK_DATA_DIR": str(tmp_path)}
    base.update(env)
    return Settings.from_env(base)


def test_parse_hex_key_accepts_plain_and_literal() -> None:
    assert parse_hex_key(HEX_KEY) == bytes.fromhex(HEX_KEY)
    assert parse_hex_key(f"x'{HEX_KEY}'") == bytes.fromhex(HEX_KEY)


@pytest.mark.parametrize("bad", ["", "zz" * 32, "ab" * 31, "ab" * 33])
def test_parse_hex_key_rejects_bad_values(bad: str) -> None:
    with pytest.raises(ArchiveKeyError):
        parse_hex_key(bad)


def test_key_to_hex_requires_32_bytes() -> None:
    assert len(key_to_hex(generate_key())) == 64
    with pytest.raises(ArchiveKeyError):
        key_to_hex(b"\x00" * 16)


def test_pragma_literal_is_a_raw_key() -> None:
    assert pragma_literal(bytes.fromhex(HEX_KEY)) == f"x'{HEX_KEY}'"


def test_generated_keys_are_unique_and_sized() -> None:
    first, second = generate_key(), generate_key()
    assert len(first) == KEY_BYTES
    assert first != second


def test_env_key_wins_over_keychain(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SESAMETOK_ARCHIVE_KEY=HEX_KEY)
    backend = FakeKeychain(stored=b"\x01" * 32)

    resolved = resolve_archive_key(settings, keychain=backend)

    assert resolved.source == "env"
    assert resolved.key == bytes.fromhex(HEX_KEY)
    assert backend.set_calls == []


def test_keychain_key_is_reused(tmp_path: Path) -> None:
    stored = b"\x02" * 32
    settings = _settings(tmp_path)

    resolved = resolve_archive_key(settings, keychain=FakeKeychain(stored=stored))

    assert resolved.source == "keychain"
    assert resolved.key == stored


def test_missing_key_is_created_and_stored(tmp_path: Path) -> None:
    backend = FakeKeychain()
    settings = _settings(tmp_path)

    resolved = resolve_archive_key(settings, keychain=backend)

    assert resolved.source == "created"
    assert resolved.is_new is True
    assert len(resolved.key) == KEY_BYTES
    assert backend.set_calls and backend.set_calls[0][2] == resolved.key


def test_keychain_failure_is_fail_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(ArchiveKeyError):
        resolve_archive_key(settings, keychain=FakeKeychain(fail_set=True))


def test_security_cli_get_returns_none_when_not_found(monkeypatch) -> None:
    class Result:
        returncode = 44
        stdout = ""
        stderr = "item not found"

    monkeypatch.setattr(
        "sesametok_backend.security.archive_key.shutil.which", lambda _: "/usr/bin/security"
    )
    monkeypatch.setattr(
        "sesametok_backend.security.archive_key.subprocess.run", lambda *a, **k: Result()
    )

    assert SecurityCliKeychain().get("svc", "acct") is None


def test_security_cli_reports_a_missing_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        "sesametok_backend.security.archive_key.shutil.which", lambda _: None
    )
    with pytest.raises(ArchiveKeyError):
        SecurityCliKeychain().get("svc", "acct")
