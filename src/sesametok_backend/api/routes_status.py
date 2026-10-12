"""헬스와 상태 엔드포인트.

``GET /health``는 슈퍼바이저가 생존 여부를 확인할 수 있도록 일부러 토큰을 요구하지 않는다.
다른 모든 엔드포인트는 Bearer 토큰을 요구한다.
"""

from __future__ import annotations

from .context import ApiContext
from .router import Request, Response


def get_health(context: ApiContext, request: Request) -> Response:
    return Response.json(context.health_payload())


def get_status(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    return Response.json(context.status_payload())
