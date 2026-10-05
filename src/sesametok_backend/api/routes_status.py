"""Health and status endpoints.

``GET /health`` is deliberately token-free so a supervisor can probe liveness;
every other endpoint requires the bearer token.
"""

from __future__ import annotations

from .context import ApiContext
from .router import Request, Response


def get_health(context: ApiContext, request: Request) -> Response:
    return Response.json(context.health_payload())


def get_status(context: ApiContext, request: Request) -> Response:
    context.require_auth(request)
    return Response.json(context.status_payload())
