"""인터페이스 API 토큰: 베어러 토큰 하나를 0600으로 저장하거나 환경변수로 받는다.

이 토큰은 하위 인터페이스 프로젝트를 인증한다. 카카오톡 데이터베이스 키와는
무관하며 로그에 절대 기록하지 않는다.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from ..config import Settings
from ..fsutil import atomic_write_text, read_text, tighten

TOKEN_BYTES = 32


def load_or_create_token(settings: Settings) -> str:
    """API 토큰을 반환한다. 최초 사용 시 생성하여 저장한다.

    우선순위: ``SESAMETOK_API_TOKEN`` → ``<data_dir>/api_token`` → 새로 생성
    (0600으로 기록).
    """

    if settings.api_token:
        return settings.api_token

    path = settings.api_token_path
    existing = read_text(path)
    if existing and existing.strip():
        tighten(path)
        return existing.strip()

    token = secrets.token_urlsafe(TOKEN_BYTES)
    atomic_write_text(path, token + "\n")
    return token


def rotate_token(settings: Settings) -> str:
    """저장된 토큰을 새것으로 교체하고 반환한다."""

    token = secrets.token_urlsafe(TOKEN_BYTES)
    atomic_write_text(settings.api_token_path, token + "\n")
    return token


def token_source(settings: Settings) -> str:
    """토큰이 어디에서 오는지, 토큰 자체는 노출하지 않고 알려준다."""

    if settings.api_token:
        return "environment"
    if read_text(settings.api_token_path):
        return "file"
    return "not created yet"


def token_path(settings: Settings) -> Path:
    return settings.api_token_path
