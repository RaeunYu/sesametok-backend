"""설정: 환경변수와 이 프로젝트가 소유하는 로컬 경로.

모든 런타임 경로는 재정의할 수 있으므로 테스트와 개발 실행이 실제 macOS 상태를
건드리지 않는다 (ADR-009). 모든 기본값은 ``sesametok`` 디렉터리 이름 아래에 있고
환경변수 접두사는 ``SESAMETOK_``이다.
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

APP_NAME = "SesametokBackend"
DEFAULT_APP_BUNDLE = Path("/Applications") / f"{APP_NAME}.app"

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "sesametok-backend"
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "sesametok-backend"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756

_TRUTHY = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """환경변수를 해석할 수 없을 때 발생한다."""


class _Env:
    """명시적인 매핑 또는 실제 환경에서 ``SESAMETOK_*``를 읽는다."""

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
            raise ConfigError(
                f"{ENV_PREFIX}{name}은(는) 정수여야 합니다. 받은 값: {value!r}"
            ) from exc

    def float(self, name: str, default: float) -> float:
        value = self.raw(name)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(
                f"{ENV_PREFIX}{name}은(는) 숫자여야 합니다. 받은 값: {value!r}"
            ) from exc

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
            raise ConfigError(
                f"{ENV_PREFIX}{name}은(는) 정수여야 합니다. 받은 값: {value!r}"
            ) from exc
        if parsed <= 0:
            raise ConfigError(
                f"{ENV_PREFIX}{name}은(는) 양의 정수여야 합니다. 받은 값: {parsed}"
            )
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
    """이 프로세스가 시작될 당시 환경의 불변 스냅숏."""

    data_dir: Path = DEFAULT_DATA_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_seconds: float = 3.0
    lookback_seconds: int = 900
    user_id_override: int | None = None
    max_sends_per_minute: int = 60
    max_sends_per_room_per_minute: int = 50
    no_menubar: bool = False
    api_enabled: bool = True
    log_level: str = "info"
    api_token: str | None = None
    archive_key_hex: str | None = None
    uds_enabled: bool = True
    uds_path_override: Path | None = None
    launchd_plist_override: Path | None = None
    app_bundle_override: Path | None = None
    event_retention_days: int = 30
    event_retention_max: int = 100_000
    backfill_days: int = 30
    backfill_max: int = 100_000
    webhook_timeout_seconds: float = 5.0
    webhook_max_attempts: int = 5

    # ---------------------------------------------------------------------- 팩터리

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
            max_sends_per_minute=env.int("MAX_SENDS_PER_MINUTE", 60),
            max_sends_per_room_per_minute=env.int("MAX_SENDS_PER_ROOM_PER_MINUTE", 50),
            no_menubar=env.boolean("NO_MENUBAR"),
            api_enabled=env.boolean("API", True),
            log_level=env.text("LOG_LEVEL", "info"),
            api_token=env.raw("API_TOKEN"),
            archive_key_hex=env.raw("ARCHIVE_KEY"),
            uds_enabled=env.boolean("UDS", True),
            uds_path_override=env.optional_path("UDS_PATH"),
            launchd_plist_override=env.optional_path("LAUNCHD_PLIST"),
            app_bundle_override=env.optional_path("APP_BUNDLE"),
            event_retention_days=env.int("EVENT_RETENTION_DAYS", 30),
            event_retention_max=env.int("EVENT_RETENTION_MAX", 100_000),
            backfill_days=env.int("BACKFILL_DAYS", 30),
            backfill_max=env.int("BACKFILL_MAX", 100_000),
            webhook_timeout_seconds=env.float("WEBHOOK_TIMEOUT_SECONDS", 5.0),
            webhook_max_attempts=env.int("WEBHOOK_MAX_ATTEMPTS", 5),
        )

    # ----------------------------------------------------------------------- 경로

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
    def uds_path(self) -> Path:
        # UNIX 소켓 경로는 길이 제한이 있으므로(macOS에서 약 104바이트), 긴 데이터
        # 디렉터리를 위해 재정의할 수 있다.
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
        # 테스트(와 대체 설치)가 실제 ~/Library/LaunchAgents에 쓰지 않도록
        # 재정의할 수 있다.
        if self.launchd_plist_override is not None:
            return self.launchd_plist_override
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    @property
    def python_executable(self) -> Path:
        """TCC가 유효하게 유지되려면 LaunchAgent가 가리켜야 하는 인터프리터."""

        return Path(sys.executable).resolve()

    @property
    def app_bundle_path(self) -> Path:
        """TCC 권한을 부여할 앱 번들 경로(``packaging/build_app.py``가 만든다)."""

        if self.app_bundle_override is not None:
            return self.app_bundle_override
        return DEFAULT_APP_BUNDLE

    @property
    def app_executable(self) -> Path:
        """앱 번들 안의 실행 파일."""

        return self.app_bundle_path / "Contents" / "MacOS" / APP_NAME

    def app_installed(self) -> bool:
        """앱 번들이 빌드되어 있는가."""

        return self.app_executable.is_file()

    @property
    def tcc_grant_target(self) -> Path:
        """사용자가 권한을 부여할 대상.

        앱이 있으면 **앱 번들**이다(권한이 앱에만 귀속되고 공용 인터프리터는 영향을
        받지 않는다). 앱이 없을 때만 인터프리터로 떨어진다.
        """

        return self.app_bundle_path if self.app_installed() else self.python_executable

    # ---------------------------------------------------------------------- 헬퍼

    def ensure_directories(self) -> None:
        """이 프로세스가 쓰는 비공개 디렉터리를 만든다."""

        ensure_private_dir(self.data_dir)
        ensure_private_dir(self.kakaotalk_auth_cache_path.parent)
        ensure_dir(self.log_dir)

    def redacted(self) -> dict[str, object]:
        """로그에 안전한 뷰: 비밀 값을 대체하며, 원본 값은 절대 담지 않는다."""

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
