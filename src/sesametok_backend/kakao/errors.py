"""카카오톡 읽기 경로의 타입이 있는 실패들.

각 오류는 대화 데이터를 노출하지 않으면서 대응에 충분한 맥락을 담는다.
"""

from __future__ import annotations


class KakaoError(RuntimeError):
    """카카오톡 읽기 경로에서 발생하는 모든 실패의 기반 클래스."""


class KakaoStoreError(KakaoError):
    """저장소 루트, 기기 UUID 또는 데이터베이스 파일을 확인할 수 없었다."""


class KakaoDatabaseNotFound(KakaoStoreError):
    """유도된 이름과 일치하는 카카오톡 데이터베이스가 없다."""


class KakaoAuthError(KakaoError):
    """데이터베이스를 열 수 있는 ``(user_id, uuid)`` 후보가 없었다."""


class KakaoCipherError(KakaoError):
    """유도된 키로 데이터베이스를 열 수 없었다."""


class KakaoWrongKeyError(KakaoCipherError):
    """데이터베이스는 열렸지만 키를 거부했다(``file is not a database``)."""


class KakaoSchemaError(KakaoError):
    """데이터베이스는 열렸지만 필요한 테이블이 없다."""
