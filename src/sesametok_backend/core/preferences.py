"""메뉴바 선택을 다음 실행까지 기억하는 작은 JSON 저장소.

지금은 폴링 주기 숫자 하나만 담는다(``<data_dir>/preferences.json``). 방 이름,
닉네임, 메시지 본문 같은 대화 데이터는 여기에 절대 들어가지 않는다. 쓰기는
:func:`sesametok_backend.fsutil.atomic_write_text`를 거치므로 중간에 프로세스가
죽어도 반쯤 쓰인 JSON이 남지 않는다. 저장 실패는 로그만 남기고 호출자의 동작을
막지 않는다 — 메뉴 선택은 이번 실행에 즉시 적용되어야 하기 때문이다.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text, read_text

logger = logging.getLogger("sesametok_backend.core.preferences")

#: 데이터 디렉터리 안의 파일 이름.
PREFERENCES_FILENAME = "preferences.json"

#: 저장 키. 값은 초 단위 숫자 하나다.
POLL_SECONDS_KEY = "poll_seconds"

#: 저장된 값도 없고 설정에도 유효한 값이 없을 때 쓰는 기본 주기(초).
DEFAULT_POLL_SECONDS = 3.0


def preferences_path(data_dir: Path) -> Path:
    """*data_dir*에 있는 환경설정 파일 경로."""

    return Path(data_dir) / PREFERENCES_FILENAME


def coerce_poll_seconds(value: object) -> float | None:
    """저장/설정된 값을 쓸 수 있는 주기(초)로 바꾼다. 비정상이면 ``None``.

    ``bool``은 숫자가 아니며, 0 이하이거나 유한하지 않은 값(``NaN``, ``inf``),
    숫자가 아닌 문자열은 모두 비정상으로 본다. 이 판정 하나를 저장값과 설정값이
    함께 쓰므로 "0 이하이거나 비정상이면 무시한다"는 규칙이 한 곳에만 있다.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def resolve_poll_seconds(
    stored: object,
    configured: object,
    *,
    default: float = DEFAULT_POLL_SECONDS,
) -> float:
    """우선순위: 저장된 값 > 설정(``SESAMETOK_POLL_SECONDS``) > 기본 3초."""

    for candidate in (stored, configured, default):
        seconds = coerce_poll_seconds(candidate)
        if seconds is not None:
            return seconds
    return DEFAULT_POLL_SECONDS


class Preferences:
    """JSON 환경설정 파일 하나를 읽고 원자적으로 갱신한다.

    파일이 없거나 깨졌으면 빈 매핑으로 취급한다(예외를 밖으로 던지지 않는다).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "Preferences":
        return cls(preferences_path(data_dir))

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ 읽기

    def read(self) -> dict[str, Any]:
        """파일 내용을 매핑으로 읽는다. 없거나 깨졌으면 ``{}``."""

        text = read_text(self._path)
        if not text:  # 없음, 읽기 실패, 빈 파일
            return {}
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            logger.warning("preferences.json을 해석하지 못했습니다; 저장된 값을 무시합니다")
            return {}
        if not isinstance(data, dict):
            logger.warning("preferences.json이 객체가 아닙니다; 저장된 값을 무시합니다")
            return {}
        return data

    def poll_seconds(self) -> float | None:
        """저장된 폴링 주기(초), 없거나 비정상이면 ``None``."""

        return coerce_poll_seconds(self.read().get(POLL_SECONDS_KEY))

    # ------------------------------------------------------------------ 쓰기

    def update(self, **values: object) -> bool:
        """기존 키를 보존한 채 *values*를 병합해 원자적으로 쓴다.

        실패하면 경고만 남기고 ``False``를 반환한다(예외를 던지지 않는다).
        """

        data = self.read()
        data.update(values)
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            atomic_write_text(self._path, text)
        except OSError as exc:
            logger.warning(
                "preferences.json을 저장하지 못했습니다 (%s); 이번 실행에는 계속 적용합니다",
                type(exc).__name__,
            )
            return False
        return True

    def set_poll_seconds(self, seconds: object) -> float | None:
        """*seconds*를 저장하고 저장된 값(초)을 반환한다.

        비정상 값이거나 저장에 실패하면 ``None``을 반환하며 예외를 던지지 않는다.
        """

        value = coerce_poll_seconds(seconds)
        if value is None:
            logger.warning("저장할 폴링 주기가 유효하지 않아 무시했습니다")
            return None
        if not self.update(**{POLL_SECONDS_KEY: value}):
            return None
        return value
