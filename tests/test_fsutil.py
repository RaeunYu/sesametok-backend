from __future__ import annotations

import os
from pathlib import Path

from sesametok_backend.fsutil import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_private_dir,
    file_mode,
    read_text,
    tighten,
)


def test_private_dir_mode(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    ensure_private_dir(target)
    assert target.is_dir()
    assert os.stat(target).st_mode & 0o777 == 0o700


def test_atomic_write_leaves_no_temp_and_correct_mode(tmp_path: Path) -> None:
    target = tmp_path / "secret" / "api_token"
    atomic_write_text(target, "hunter2\n")

    assert target.read_text(encoding="utf-8") == "hunter2\n"
    assert file_mode(target) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["api_token"]


def test_atomic_write_replaces_content(tmp_path: Path) -> None:
    target = tmp_path / "x"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"


def test_read_text_defaults(tmp_path: Path) -> None:
    assert read_text(tmp_path / "missing") is None
    assert read_text(tmp_path / "missing", "fallback") == "fallback"


def test_tighten_and_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "f"
    atomic_write_text(target, "x", mode=0o644)
    assert file_mode(target) == 0o644
    assert tighten(target, 0o600) is True
    assert file_mode(target) == 0o600


def test_atomic_write_bytes(tmp_path: Path) -> None:
    target = tmp_path / "blob"
    atomic_write_bytes(target, b"\x00\x01", mode=0o600)
    assert target.read_bytes() == b"\x00\x01"
