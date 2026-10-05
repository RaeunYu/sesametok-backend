from __future__ import annotations

import os
from pathlib import Path

from sesametok_backend.launcher import ProcessLock


def test_acquire_writes_pid_and_is_held(tmp_path: Path) -> None:
    lock = ProcessLock(tmp_path / "backend.lock")

    assert lock.acquire() is True
    assert lock.path.is_file()
    assert lock.owner_pid() == os.getpid()
    assert ProcessLock.is_held(lock.path) is True
    assert os.stat(lock.path).st_mode & 0o777 == 0o600

    lock.release()
    assert ProcessLock.is_held(lock.path) is False
    assert not lock.path.exists()


def test_second_lock_in_same_process_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "backend.lock"
    first = ProcessLock(path)
    second = ProcessLock(path)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()


def test_is_held_on_missing_file_is_false(tmp_path: Path) -> None:
    assert ProcessLock.is_held(tmp_path / "nope.lock") is False


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = ProcessLock(tmp_path / "backend.lock")
    lock.acquire()
    lock.release()
    lock.release()
    assert ProcessLock.is_held(lock.path) is False


def test_owner_pid_none_when_absent(tmp_path: Path) -> None:
    assert ProcessLock(tmp_path / "backend.lock").owner_pid() is None
