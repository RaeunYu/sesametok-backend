from __future__ import annotations

from pathlib import Path

import pytest

from sesametok_backend.config import (
    BUNDLE_ID,
    ConfigError,
    DEFAULT_HOST,
    DEFAULT_PORT,
    LAUNCHD_LABEL,
    Settings,
)


def test_defaults_are_local_and_personal() -> None:
    settings = Settings.from_env({})

    assert settings.host == DEFAULT_HOST
    assert settings.port == DEFAULT_PORT
    assert settings.poll_seconds == 3.0
    assert settings.lookback_seconds == 900
    assert settings.uds_enabled is True
    assert settings.max_sends_per_minute == 6
    assert settings.event_retention_days == 30
    assert settings.event_retention_max == 100_000
    assert settings.backfill_days == 30
    assert settings.backfill_max == 100_000


def test_identifiers_use_the_project_name() -> None:
    assert LAUNCHD_LABEL == "com.sesametok.sesametok-backend"
    assert BUNDLE_ID == LAUNCHD_LABEL


def test_env_overrides(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "SESAMETOK_DATA_DIR": str(tmp_path / "d"),
            "SESAMETOK_HOST": "127.0.0.1",
            "SESAMETOK_PORT": "9000",
            "SESAMETOK_POLL_SECONDS": "1.5",
            "SESAMETOK_USER_ID": "1234",
            "SESAMETOK_NO_MENUBAR": "1",
            "SESAMETOK_EVENT_RETENTION_MAX": "5",
        }
    )

    assert settings.data_dir == tmp_path / "d"
    assert settings.port == 9000
    assert settings.poll_seconds == 1.5
    assert settings.user_id_override == 1234
    assert settings.no_menubar is True
    assert settings.event_retention_max == 5


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_env_falls_back_to_default(raw: str) -> None:
    settings = Settings.from_env({"SESAMETOK_PORT": raw})
    assert settings.port == DEFAULT_PORT


def test_invalid_integer_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"SESAMETOK_PORT": "http"})


def test_negative_user_id_is_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"SESAMETOK_USER_ID": "0"})


def test_boolean_flags_accept_common_spellings() -> None:
    assert Settings.from_env({"SESAMETOK_NO_MENUBAR": "true"}).no_menubar is True
    assert Settings.from_env({"SESAMETOK_NO_MENUBAR": "no"}).no_menubar is False
    assert Settings.from_env({"SESAMETOK_UDS": "0"}).uds_enabled is False


def test_paths_are_derived_from_data_dir(tmp_path: Path) -> None:
    settings = Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)})

    assert settings.archive_path == tmp_path / "archive.sqlite3"
    assert settings.api_token_path == tmp_path / "api_token"
    assert settings.uds_path == tmp_path / "backend.sock"
    assert settings.kakaotalk_auth_cache_path == tmp_path / "auth" / "kakao.json"


def test_redacted_never_exposes_secrets(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "SESAMETOK_DATA_DIR": str(tmp_path),
            "SESAMETOK_API_TOKEN": "super-secret-token",
            "SESAMETOK_ARCHIVE_KEY": "a" * 64,
        }
    )
    view = settings.redacted()

    assert "super-secret-token" not in str(view)
    assert "a" * 64 not in str(view)
    assert view["api_token"] == "set"
    assert view["archive_key"] == "env"


def test_ensure_directories_creates_private_dirs(tmp_path: Path) -> None:
    import os

    settings = Settings.from_env(
        {"SESAMETOK_DATA_DIR": str(tmp_path / "data"), "SESAMETOK_LOG_DIR": str(tmp_path / "logs")}
    )
    settings.ensure_directories()

    assert settings.data_dir.is_dir()
    assert settings.kakaotalk_auth_cache_path.parent.is_dir()
    assert settings.log_dir.is_dir()
    assert os.stat(settings.data_dir).st_mode & 0o777 == 0o700


def test_grant_target_falls_back_to_the_interpreter(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "SESAMETOK_DATA_DIR": str(tmp_path),
            "SESAMETOK_APP_BUNDLE": str(tmp_path / "Missing.app"),
        }
    )
    assert settings.app_installed() is False
    assert settings.tcc_grant_target == settings.python_executable


def test_grant_target_uses_the_app_bundle_when_present(tmp_path: Path) -> None:
    app = tmp_path / "SesametokBackend.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "SesametokBackend").write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings.from_env(
        {"SESAMETOK_DATA_DIR": str(tmp_path), "SESAMETOK_APP_BUNDLE": str(app)}
    )
    assert settings.app_installed() is True
    assert settings.tcc_grant_target == app
