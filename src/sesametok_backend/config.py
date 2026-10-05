"""Configuration: environment variables and the local paths this project owns.

Every runtime path is overridable so tests and development runs never touch the
real macOS state (ADR-009). Every default lives under the ``sesametok``
directory names and the environment prefix is ``SESAMETOK_``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .fsutil import ensure_dir, ensure_private_dir

ENV_PREFIX = "SESAMETOK_"

LAUNCHD_LABEL = "com.sesametok.sesametok-backend"
BUNDLE_ID = LAUNCHD_LABEL

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "sesametok-backend"
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "sesametok-backend"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756

_TRUTHY = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """Raised when an environment variable cannot be interpreted."""


class _Env:
    """Reads ``SESAMETOK_*`` from an explicit mapping, or the real environment."""

    def __init__(self, source: Mapping[str, str] | None = None) -> None:
        self._source = source

    def raw(self, name: str) -> str | None:
        key = ENV_PREFIX + name
        value = self._source.get(key) if self._source is not None else os.environ.get(key)
        if value is None:
            return None
        value = value.strip()
        return value or None

    def text(self, name: str, default: str) -> str:
        value = self.raw(name)
        return default if value is None else value

    def int(self, name: str, default: int) -> int:
        value = self.raw(name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{name} must be an integer, got {value!r}") from exc

    def float(self, name: str, default: float) -> float:
        value = self.raw(name)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{name} must be a number, got {value!r}") from exc

    def boolean(self, name: str, default: bool = False) -> bool:
        value = self.raw(name)
        if value is None:
            return default
        return value.lower() in _TRUTHY

    def positive_int(self, name: str) -> int | None:
        value = self.raw(name)
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ConfigError(f"{ENV_PREFIX}{name} must be an integer, got {value!r}") from exc
        if parsed <= 0:
            raise ConfigError(f"{ENV_PREFIX}{name} must be a positive integer, got {parsed}")
        return parsed

    def path(self, name: str, default: Path) -> Path:
        value = self.raw(name)
        if value is None:
            return default
        return Path(value).expanduser()

    def optional_path(self, name: str) -> Path | None:
        value = self.raw(name)
        return None if value is None else Path(value).expanduser()


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the environment this process was started with."""

    data_dir: Path = DEFAULT_DATA_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_seconds: float = 3.0
    lookback_seconds: int = 900
    user_id_override: int | None = None
    max_sends_per_minute: int = 6
    max_sends_per_room_per_minute: int = 3
    no_menubar: bool = False
    api_enabled: bool = True
    log_level: str = "info"
    api_token: str | None = None
    archive_key_hex: str | None = None
    uds_enabled: bool = True
    uds_path_override: Path | None = None
    launchd_plist_override: Path | None = None
    event_retention_days: int = 30
    event_retention_max: int = 100_000
    backfill_days: int = 30
    backfill_max: int = 100_000
    webhook_timeout_seconds: float = 5.0
    webhook_max_attempts: int = 5

    # ---------------------------------------------------------------- factories

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = _Env(environ)
        return cls(
            data_dir=env.path("DATA_DIR", DEFAULT_DATA_DIR),
            log_dir=env.path("LOG_DIR", DEFAULT_LOG_DIR),
            host=env.text("HOST", DEFAULT_HOST),
            port=env.int("PORT", DEFAULT_PORT),
            poll_seconds=env.float("POLL_SECONDS", 3.0),
            lookback_seconds=env.int("LOOKBACK_SECONDS", 900),
            user_id_override=env.positive_int("USER_ID"),
            max_sends_per_minute=env.int("MAX_SENDS_PER_MINUTE", 6),
            max_sends_per_room_per_minute=env.int("MAX_SENDS_PER_ROOM_PER_MINUTE", 3),
            no_menubar=env.boolean("NO_MENUBAR"),
            api_enabled=env.boolean("API", True),
            log_level=env.text("LOG_LEVEL", "info"),
            api_token=env.raw("API_TOKEN"),
            archive_key_hex=env.raw("ARCHIVE_KEY"),
            uds_enabled=env.boolean("UDS", True),
            uds_path_override=env.optional_path("UDS_PATH"),
            launchd_plist_override=env.optional_path("LAUNCHD_PLIST"),
            event_retention_days=env.int("EVENT_RETENTION_DAYS", 30),
            event_retention_max=env.int("EVENT_RETENTION_MAX", 100_000),
            backfill_days=env.int("BACKFILL_DAYS", 30),
            backfill_max=env.int("BACKFILL_MAX", 100_000),
            webhook_timeout_seconds=env.float("WEBHOOK_TIMEOUT_SECONDS", 5.0),
            webhook_max_attempts=env.int("WEBHOOK_MAX_ATTEMPTS", 5),
        )

    # -------------------------------------------------------------------- paths

    @property
    def archive_path(self) -> Path:
        return self.data_dir / "archive.sqlite3"

    @property
    def kakaotalk_auth_cache_path(self) -> Path:
        return self.data_dir / "auth" / "kakao.json"

    @property
    def api_token_path(self) -> Path:
        return self.data_dir / "api_token"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def uds_path(self) -> Path:
        # UNIX socket paths are length-limited (~104 bytes on macOS), so this is
        # overridable for long data directories.
        if self.uds_path_override is not None:
            return self.uds_path_override
        return self.data_dir / "backend.sock"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "backend.lock"

    @property
    def backend_log_path(self) -> Path:
        return self.log_dir / "backend.log"

    @property
    def launchd_plist_path(self) -> Path:
        # Overridable so tests (and alternate installs) never write into the
        # real ~/Library/LaunchAgents.
        if self.launchd_plist_override is not None:
            return self.launchd_plist_override
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    @property
    def python_executable(self) -> Path:
        """The interpreter the LaunchAgent must point at for TCC to stay valid."""

        return Path(sys.executable).resolve()

    # ----------------------------------------------------------------- helpers

    def ensure_directories(self) -> None:
        """Create the private directories this process writes to."""

        ensure_private_dir(self.data_dir)
        ensure_private_dir(self.outbox_dir)
        ensure_private_dir(self.kakaotalk_auth_cache_path.parent)
        ensure_dir(self.log_dir)

    def redacted(self) -> dict[str, object]:
        """A log-safe view: secrets replaced, never the raw values."""

        return {
            "data_dir": str(self.data_dir),
            "log_dir": str(self.log_dir),
            "host": self.host,
            "port": self.port,
            "poll_seconds": self.poll_seconds,
            "lookback_seconds": self.lookback_seconds,
            "no_menubar": self.no_menubar,
            "api_enabled": self.api_enabled,
            "uds_enabled": self.uds_enabled,
            "api_token": "set" if self.api_token else "unset",
            "archive_key": "env" if self.archive_key_hex else "unset",
            "event_retention": f"{self.event_retention_days}d/{self.event_retention_max}",
            "backfill": f"{self.backfill_days}d/{self.backfill_max}",
        }
