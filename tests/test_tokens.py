from __future__ import annotations

import os
from pathlib import Path

from sesametok_backend.config import Settings
from sesametok_backend.security.tokens import (
    load_or_create_token,
    rotate_token,
    token_source,
)


def _settings(tmp_path: Path, **env: str) -> Settings:
    base = {"SESAMETOK_DATA_DIR": str(tmp_path)}
    base.update(env)
    return Settings.from_env(base)


def test_token_is_created_private_and_reused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    first = load_or_create_token(settings)
    assert len(first) >= 32
    assert token_source(settings) == "file"

    path = settings.api_token_path
    assert path.is_file()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8").strip() == first

    assert load_or_create_token(settings) == first


def test_env_token_wins_over_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SESAMETOK_API_TOKEN="from-env")
    assert load_or_create_token(settings) == "from-env"
    assert token_source(settings) == "environment"
    assert not settings.api_token_path.exists()


def test_rotate_replaces_stored_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = load_or_create_token(settings)
    second = rotate_token(settings)

    assert second != first
    assert load_or_create_token(settings) == second


def test_token_source_before_creation(tmp_path: Path) -> None:
    assert token_source(_settings(tmp_path)) == "not created yet"
