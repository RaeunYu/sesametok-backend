"""Typed failures for the KakaoTalk read path.

Each error carries enough context to act on without leaking conversation data.
"""

from __future__ import annotations


class KakaoError(RuntimeError):
    """Base class for every failure originating in the KakaoTalk read path."""


class KakaoStoreError(KakaoError):
    """Storage root, device UUID, or database file could not be resolved."""


class KakaoDatabaseNotFound(KakaoStoreError):
    """No KakaoTalk database matching the derived name exists."""


class KakaoAuthError(KakaoError):
    """No ``(user_id, uuid)`` candidate could open the database."""


class KakaoCipherError(KakaoError):
    """The database could not be opened with the derived key."""


class KakaoWrongKeyError(KakaoCipherError):
    """The database opened but rejected the key (``file is not a database``)."""


class KakaoSchemaError(KakaoError):
    """The database opened but a required table is missing."""
