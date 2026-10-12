"""안정적인 API 오류.

모든 실패는 HTTP 상태 코드와 함께 ``{"error": {"code", "message", "retryable"}}``
형태의 JSON 본문으로 나간다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:  # pragma: no cover - 디버깅 보조
        return f"{self.status} {self.code}: {self.message}"

    def to_payload(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            }
        }


def bad_request(message: str, code: str = "bad_request") -> ApiError:
    return ApiError(400, code, message)


def unauthorized(message: str = "Bearer 토큰이 없거나 올바르지 않습니다") -> ApiError:
    return ApiError(401, "unauthorized", message)


def not_found(message: str, code: str = "not_found") -> ApiError:
    return ApiError(404, code, message)


def conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def payload_too_large(message: str = "요청 본문이 너무 큽니다") -> ApiError:
    return ApiError(413, "payload_too_large", message)


def not_implemented(message: str, code: str = "not_implemented") -> ApiError:
    return ApiError(501, code, message)


def internal(message: str = "내부 오류") -> ApiError:
    return ApiError(500, "internal_error", message, retryable=True)
