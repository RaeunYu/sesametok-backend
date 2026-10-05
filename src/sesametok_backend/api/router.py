"""A tiny path router for the loopback API.

Patterns use ``{name}`` segments (e.g. ``/v1/rooms/{chat_id}/messages``). No
third-party framework is involved; the route table is explicit and greppable.
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
            raise bad_request("a JSON object body is required")
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise bad_request(f"body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise bad_request("body must be a JSON object")
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
                f"{method} is not allowed here; try: {', '.join(sorted(allowed))}",
            )
        raise _missing(path)


def _missing(path: str):
    from .errors import not_found

    return not_found(f"no route for {path}", "route_not_found")
