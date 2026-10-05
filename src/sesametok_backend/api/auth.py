"""Bearer-token authentication for the interface API.

The token only guards the loopback API; it is compared in constant time and never
appears in logs.
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
    """Raise ``unauthorized`` unless the header carries the expected token."""

    if not expected:
        raise unauthorized("this backend has no API token configured")
    provided = extract_bearer(header)
    if provided is None or not hmac.compare_digest(provided, expected):
        raise unauthorized()
