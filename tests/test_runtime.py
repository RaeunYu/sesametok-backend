from __future__ import annotations

import os
import time

import pytest

from sesametok_backend.config import Settings
from sesametok_backend.core.archive import Archive
from sesametok_backend.kakao.auth import AccountIdentity
from sesametok_backend.kakao.errors import KakaoAuthError
from sesametok_backend.kakao.reader import Snapshot
from sesametok_backend.runtime import (
    AlreadyRunningError,
    Runtime,
    StartupError,
    running_pid,
)
from sesametok_backend.security.archive_key import ArchiveKey, ArchiveKeyError
from support import SYNTHETIC_UUID

KEY = bytes.fromhex("ab" * 32)
USER_ID = 123456789


class QuietSource:
    """A source that always reports an empty snapshot."""

    def __init__(self) -> None:
        self.calls = 0

    def read_snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        self.calls += 1
        return Snapshot(rooms=(), messages=(), skipped_rows=0, open_chat_ids=frozenset())


def _settings(**overrides) -> Settings:
    base = dict(poll_seconds=0.01)
    base.update(overrides)
    return Settings(**base)


def _runtime(settings: Settings, **overrides) -> Runtime:
    kwargs: dict = dict(
        identity_resolver=lambda _settings: AccountIdentity(USER_ID, SYNTHETIC_UUID, "test"),
        archive_key_resolver=lambda _settings: ArchiveKey(key=KEY, source="env"),
        source=QuietSource(),
        interactive=False,
    )
    kwargs.update(overrides)
    return Runtime(settings, **kwargs)


def test_start_creates_token_lock_and_archive(env) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)

    runtime.start()
    try:
        assert runtime.api_token
        assert os.stat(settings.api_token_path).st_mode & 0o777 == 0o600
        assert settings.lock_path.is_file()
        assert settings.archive_path.is_file()
        assert runtime.account_hash is not None
        assert runtime.poller is not None
        assert running_pid(settings) == os.getpid()

        deadline = time.time() + 3
        while time.time() < deadline and runtime.poller.stats.polls < 1:
            time.sleep(0.02)
        assert runtime.poller.stats.polls >= 1
    finally:
        runtime.shutdown()

    assert not settings.lock_path.exists()
    assert running_pid(settings) is None


def test_second_runtime_refuses_to_start(env) -> None:
    settings = Settings.from_env()
    first = _runtime(settings)
    first.start()
    try:
        with pytest.raises(AlreadyRunningError):
            _runtime(settings).start()
    finally:
        first.shutdown()


def test_shutdown_runs_hooks_once_in_reverse(env) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)
    calls: list[str] = []
    runtime.add_shutdown_hook(lambda: calls.append("first"))
    runtime.add_shutdown_hook(lambda: calls.append("second"))

    runtime.start()
    runtime.shutdown()

    assert calls == ["second", "first"]


def test_a_failing_hook_does_not_block_shutdown(env) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)
    ran: list[str] = []

    def boom() -> None:
        raise RuntimeError("nope")

    runtime.add_shutdown_hook(boom)
    runtime.add_shutdown_hook(lambda: ran.append("after"))

    runtime.start()
    runtime.shutdown()

    assert ran == ["after"]
    assert not settings.lock_path.exists()


def test_unresolvable_identity_is_a_startup_error(env) -> None:
    settings = Settings.from_env()

    def explode(_settings: Settings) -> AccountIdentity:
        raise KakaoAuthError("no candidate worked")

    runtime = _runtime(settings, identity_resolver=explode)

    with pytest.raises(StartupError):
        runtime.start()

    # The lock must not be left behind by a failed start.
    assert not settings.lock_path.exists()


def test_missing_archive_key_is_a_startup_error(env) -> None:
    settings = Settings.from_env()

    def explode(_settings: Settings) -> ArchiveKey:
        raise ArchiveKeyError("keychain unavailable")

    runtime = _runtime(settings, archive_key_resolver=explode)

    with pytest.raises(StartupError):
        runtime.start()

    assert not settings.lock_path.exists()


def test_health_reports_archive_and_polling(env) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)
    runtime.start()
    try:
        health = runtime.health()
        assert health["running"] is True
        assert health["archive_ready"] is True
        assert health["polling"] is True
        assert health["consecutive_failures"] == 0
    finally:
        runtime.shutdown()


def test_archive_factory_opens_readable_connections(env) -> None:
    settings = Settings.from_env()
    runtime = _runtime(settings)
    runtime.start()
    try:
        connection = runtime.archive_factory()()
        try:
            assert connection.count_messages() == 0
        finally:
            connection.close()
    finally:
        runtime.shutdown()


def test_new_key_is_not_printed_when_not_interactive(env, capsys) -> None:
    settings = Settings.from_env()
    runtime = _runtime(
        settings,
        archive_key_resolver=lambda _settings: ArchiveKey(key=KEY, source="created"),
        interactive=False,
    )
    runtime.start()
    try:
        captured = capsys.readouterr()
        assert KEY.hex() not in captured.err
        assert KEY.hex() not in captured.out
    finally:
        runtime.shutdown()
