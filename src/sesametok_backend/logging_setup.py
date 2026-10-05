"""Logging setup.

Hard rule: logs carry counts, ids, states, and character counts only — never
message bodies, room names, nicknames, or derived keys.
As a backstop, :class:`RedactLongHexFilter` rewrites long hex/base64-looking runs
so an accidental secret cannot reach disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 64+ hex chars (SQLCipher keys, PBKDF2 output) and long base64 runs.
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{64,}\b")
_LONG_B64 = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")


class RedactLongHexFilter(logging.Filter):
    """Replace secret-shaped tokens in a rendered message before it is stored."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _LONG_B64.sub("[redacted]", _LONG_HEX.sub("[redacted]", record.msg))
        return True


def setup_logging(
    level: str = "info",
    log_path: Path | None = None,
    foreground: bool = False,
) -> logging.Logger:
    """Configure the root logger. Idempotent: repeated calls replace handlers."""

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
            # A missing log directory must never stop the backend from starting.
            pass

    if foreground or log_path is None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        root.addHandler(stream)

    return logging.getLogger("sesametok_backend")
