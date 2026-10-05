"""Interface API token: one bearer token, stored 0600, or supplied by env.

The token authenticates the downstream interface projects. It is unrelated to the
KakaoTalk database key, and it is never written to logs.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from ..config import Settings
from ..fsutil import atomic_write_text, read_text, tighten

TOKEN_BYTES = 32


def load_or_create_token(settings: Settings) -> str:
    """Return the API token, generating and persisting one on first use.

    Precedence: ``SESAMETOK_API_TOKEN`` → ``<data_dir>/api_token`` → freshly
    generated (written 0600).
    """

    if settings.api_token:
        return settings.api_token

    path = settings.api_token_path
    existing = read_text(path)
    if existing and existing.strip():
        tighten(path)
        return existing.strip()

    token = secrets.token_urlsafe(TOKEN_BYTES)
    atomic_write_text(path, token + "\n")
    return token


def rotate_token(settings: Settings) -> str:
    """Replace the stored token with a fresh one and return it."""

    token = secrets.token_urlsafe(TOKEN_BYTES)
    atomic_write_text(settings.api_token_path, token + "\n")
    return token


def token_source(settings: Settings) -> str:
    """Where the token would come from, without revealing it."""

    if settings.api_token:
        return "environment"
    if read_text(settings.api_token_path):
        return "file"
    return "not created yet"


def token_path(settings: Settings) -> Path:
    return settings.api_token_path
