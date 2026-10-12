"""로깅 설정.

엄격한 규칙: 로그에는 개수, id, 상태, 문자 수만 남긴다 — 메시지 본문, 방 이름,
닉네임, 유도된 키는 절대 남기지 않는다.
안전장치로 :class:`RedactLongHexFilter`가 hex/base64처럼 길게 이어지는 문자열을
다시 쓰므로, 실수로 들어간 비밀 값이 디스크에 닿을 수 없다.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 64자 이상의 hex 문자(SQLCipher 키, PBKDF2 출력)와 길게 이어지는 base64 문자열.
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{64,}\b")
_LONG_B64 = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")


class RedactLongHexFilter(logging.Filter):
    """저장되기 전에 렌더링된 메시지에서 비밀 값 형태의 토큰을 바꾼다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _LONG_B64.sub("[redacted]", _LONG_HEX.sub("[redacted]", record.msg))
        return True


def setup_logging(
    level: str = "info",
    log_path: Path | None = None,
    foreground: bool = False,
) -> logging.Logger:
    """루트 로거를 설정한다. 멱등적이다: 반복 호출은 핸들러를 교체한다."""

    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT)
    redactor = RedactLongHexFilter()

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)
        except OSError:
            # 로그 디렉터리가 없어도 백엔드 시작을 막아서는 안 된다.
            pass

    if foreground or log_path is None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        root.addHandler(stream)

    return logging.getLogger("sesametok_backend")
