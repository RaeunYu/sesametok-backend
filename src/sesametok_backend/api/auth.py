"""인터페이스 API의 Bearer 토큰 인증.

이 토큰은 루프백 API만을 지킨다. 상수 시간으로 비교하며 로그에는 절대 나타나지 않는다.
"""

from __future__ import annotations

import hmac

from .errors import unauthorized

SCHEME = "bearer"


def extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != SCHEME:
        return None
    token = parts[1].strip()
    return token or None


def authorize(expected: str | None, header: str | None) -> None:
    """헤더가 기대한 토큰을 담고 있지 않으면 ``unauthorized``를 발생시킨다."""

    if not expected:
        raise unauthorized("이 백엔드에는 API 토큰이 설정되어 있지 않습니다")
    provided = extract_bearer(header)
    if provided is None or not hmac.compare_digest(provided, expected):
        raise unauthorized()
