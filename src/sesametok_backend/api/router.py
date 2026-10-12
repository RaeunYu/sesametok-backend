"""루프백 API용 작은 경로 라우터.

패턴은 ``{name}`` 세그먼트를 쓴다(예: ``/v1/rooms/{chat_id}/messages``). 서드파티
프레임워크는 쓰지 않는다. 라우트 테이블은 명시적이고 grep하기 쉽다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import bad_request

_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class Request:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes = b""
    params: dict[str, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return urlsplit(self.target).path

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.target).query, keep_blank_values=True)

    def query_one(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None

    def header(self, name: str, default: str | None = None) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default

    def json(self) -> dict:
        if not self.body:
            raise bad_request("JSON 객체 본문이 필요합니다")
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise bad_request(f"본문이 올바른 JSON이 아닙니다: {exc}") from exc
        if not isinstance(payload, dict):
            raise bad_request("본문은 JSON 객체여야 합니다")
        return payload


@dataclass
class Response:
    status: int = 200
    body: object | None = None
    headers: dict[str, str] = field(default_factory=dict)
    stream: Callable[[object], None] | None = None

    @classmethod
    def json(cls, body: object, status: int = 200) -> "Response":
        return cls(status=status, body=body)

    @classmethod
    def no_content(cls) -> "Response":
        return cls(status=204, body=None)


Handler = Callable[..., Response]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    regex: re.Pattern[str]
    handler: Handler


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = re.compile(
            "^" + _PARAM.sub(lambda m: f"(?P<{m.group(1)}>[^/]+)", pattern) + "$"
        )
        self._routes.append(Route(method.upper(), pattern, regex, handler))

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]]:
        decoded = unquote(path)
        allowed: set[str] = set()
        for route in self._routes:
            found = route.regex.match(decoded)
            if not found:
                continue
            if route.method != method.upper():
                allowed.add(route.method)
                continue
            return route.handler, {k: unquote(v) for k, v in found.groupdict().items()}
        if allowed:
            from .errors import ApiError

            raise ApiError(
                405,
                "method_not_allowed",
                f"{method} 메서드는 여기서 허용되지 않습니다; 사용 가능: {', '.join(sorted(allowed))}",
            )
        raise _missing(path)


def _missing(path: str):
    from .errors import not_found

    return not_found(f"{path}에 해당하는 라우트가 없습니다", "route_not_found")
