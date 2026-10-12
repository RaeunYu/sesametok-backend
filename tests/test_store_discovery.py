from __future__ import annotations

import os
from pathlib import Path

import pytest

from sesametok_backend.kakao import store
from sesametok_backend.kakao.errors import KakaoDatabaseNotFound
from support import SYNTHETIC_UUID, SYNTHETIC_USER_ID, write_synthetic_store

NAME_A = "a" * 78
NAME_B = "b" * 78


def _touch(path: Path, mtime: float, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_same_name_newer_copy_wins(tmp_path: Path) -> None:
    old = _touch(tmp_path / "old" / NAME_A, 1_000)
    new = _touch(tmp_path / "new" / NAME_A, 2_000)

    found = store.discover_databases((tmp_path / "old", tmp_path / "new"))

    assert [db.path for db in found] == [new]
    assert old.exists()


def test_non_empty_wal_mtime_counts_as_newer(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _touch(root_a / NAME_A, 1_000)
    _touch(root_b / NAME_A, 1_100)
    _touch(root_a / f"{NAME_A}-wal", 5_000, size=128)  # 커밋된 프레임이 남아 있음

    found = store.discover_databases((root_a, root_b))

    assert len(found) == 1
    assert found[0].path.parent == root_a
    assert found[0].has_pending_wal is True


def test_empty_wal_is_not_a_freshness_signal(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _touch(root_a / NAME_A, 1_000)
    _touch(root_b / NAME_A, 1_100)
    _touch(root_a / f"{NAME_A}-wal", 9_000, size=0)

    found = store.discover_databases((root_a, root_b))

    assert found[0].path.parent == root_b


def test_distinct_names_are_all_returned(tmp_path: Path) -> None:
    _touch(tmp_path / NAME_A, 1_000)
    _touch(tmp_path / NAME_B, 1_000)

    found = store.discover_databases((tmp_path,))

    assert sorted(db.name for db in found) == [NAME_A, NAME_B]


def test_non_matching_names_are_ignored(tmp_path: Path) -> None:
    _touch(tmp_path / NAME_A, 1_000)
    _touch(tmp_path / ("a" * 77), 1_000)      # 너무 짧음
    _touch(tmp_path / ("a" * 79), 1_000)      # 너무 김
    _touch(tmp_path / ("z" * 78), 1_000)      # 16진수가 아님
    _touch(tmp_path / f"{NAME_A}-wal", 1_000)
    _touch(tmp_path / f"{NAME_A}-shm", 1_000)

    found = store.discover_databases((tmp_path,))

    assert [db.name for db in found] == [NAME_A]


def test_suffix_dot_db_is_a_candidate(tmp_path: Path) -> None:
    _touch(tmp_path / f"{NAME_A}.db", 1_000)
    assert [db.name for db in store.discover_databases((tmp_path,))] == [f"{NAME_A}.db"]


def test_missing_roots_are_skipped(tmp_path: Path) -> None:
    assert store.discover_databases((tmp_path / "nope",)) == ()


def test_find_database_uses_the_derived_name(tmp_path: Path) -> None:
    written = write_synthetic_store(tmp_path)

    found = store.find_database(SYNTHETIC_USER_ID, SYNTHETIC_UUID, (tmp_path,))

    assert found is not None
    assert found.path == written


def test_find_database_accepts_the_dot_db_variant(tmp_path: Path) -> None:
    written = write_synthetic_store(tmp_path, suffix=".db")

    found = store.find_database(SYNTHETIC_USER_ID, SYNTHETIC_UUID, (tmp_path,))

    assert found is not None
    assert found.path == written


def test_require_database_raises_when_absent(tmp_path: Path) -> None:
    with pytest.raises(KakaoDatabaseNotFound):
        store.require_database(SYNTHETIC_USER_ID, SYNTHETIC_UUID, (tmp_path,))


def test_effective_mtime_prefers_pending_wal(tmp_path: Path) -> None:
    main = _touch(tmp_path / NAME_A, 1_000)
    _touch(tmp_path / f"{NAME_A}-wal", 2_500, size=64)

    database = store.inspect_database(main)

    assert database.effective_mtime == 2_500
    assert database.mtime == 1_000
