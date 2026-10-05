"""Stable API errors.

Every failure is a JSON body ``{"error": {"code", "message", "retryable"}}`` with
an HTTP status.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:  # pragma: no cover - debugging aid
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


def unauthorized(message: str = "missing or invalid bearer token") -> ApiError:
    return ApiError(401, "unauthorized", message)


def not_found(message: str, code: str = "not_found") -> ApiError:
    return ApiError(404, code, message)


def conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def payload_too_large(message: str = "request body is too large") -> ApiError:
    return ApiError(413, "payload_too_large", message)


def not_implemented(message: str, code: str = "not_implemented") -> ApiError:
    return ApiError(501, code, message)


def internal(message: str = "internal error") -> ApiError:
    return ApiError(500, "internal_error", message, retryable=True)
