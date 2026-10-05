"""Resolving which ``(user_id, uuid)`` owns the local database.

Candidates are tried in priority order and each one must *prove* itself by
unlocking the database. The winning pair is cached; the
passphrase never is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..config import Settings
from ..fsutil import atomic_write_text, read_text
from . import cipher, plist_util
from .derive import secure_key, validate_uuid
from .errors import KakaoAuthError
from .store import device_uuid, find_database

logger = logging.getLogger("sesametok_backend.kakao.auth")

AUTH_CACHE_VERSION = 1
EXTERNAL_AUTH_CACHE = Path.home() / ".cache" / "k-skill" / "kakaotalk-mac-auth.json"
REVERSE_LOOKUP_LIMIT = 1_000_000_000
REVERSE_LOOKUP_CHUNK = 250_000


@dataclass(frozen=True)
class AccountIdentity:
    user_id: int
    uuid: str
    source: str


# ---------------------------------------------------------------------- caching


def read_auth_cache(path: Path) -> AccountIdentity | None:
    """Read ``{"user_id": ..., "uuid": ...}``; malformed caches are ignored."""

    raw = read_text(path)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id")
    uuid = payload.get("uuid")
    if not isinstance(user_id, int) or user_id <= 0:
        return None
    if not isinstance(uuid, str):
        return None
    try:
        validate_uuid(uuid)
    except ValueError:
        return None
    return AccountIdentity(user_id=user_id, uuid=uuid, source="cache")


def write_auth_cache(path: Path, identity: AccountIdentity) -> None:
    """Persist the identity 0600 via an atomic rename. Never stores the key."""

    payload = {"version": AUTH_CACHE_VERSION, "user_id": identity.user_id, "uuid": identity.uuid}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False) + "\n")


def read_external_user_ids(path: Path | None = None) -> list[int]:
    """User ids from the k-skill cache, if that file happens to exist."""

    source = path if path is not None else EXTERNAL_AUTH_CACHE
    raw = read_text(source)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    value = payload.get("user_id")
    if isinstance(value, bool):
        return []
    if isinstance(value, int) and value > 0:
        return [value]
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return []
        return [parsed] if parsed > 0 else []
    return []


# ------------------------------------------------------------- reverse lookup


def _scan_range(args: tuple[int, int, frozenset[str]]) -> int | None:
    start, end, targets = args
    digest = hashlib.sha512
    for candidate in range(start, end):
        if digest(str(candidate).encode("ascii")).hexdigest() in targets:
            return candidate
    return None


def reverse_lookup(
    target_hashes: Iterable[str],
    *,
    limit: int | None = None,
    workers: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int | None:
    """Brute-force the user id whose ``sha512(str(user_id))`` is in *target_hashes*.

    Expensive by construction; only reached when every cheaper source failed.
    ``fork`` is preferred when available so workers do not re-import the entry
    point; this runs during startup, before any worker threads exist. A
    ``should_stop`` callback lets a signal abort the search promptly.
    """

    targets = frozenset(target_hashes)
    bound = REVERSE_LOOKUP_LIMIT if limit is None else limit
    if not targets or bound <= 0:
        return None

    def stopping() -> bool:
        return should_stop is not None and should_stop()

    worker_count = workers if workers is not None else (os.cpu_count() or 1)
    ranges = [
        (start, min(start + REVERSE_LOOKUP_CHUNK, bound), targets)
        for start in range(0, bound, REVERSE_LOOKUP_CHUNK)
    ]

    if worker_count <= 1:
        for chunk in ranges:
            if stopping():
                return None
            found = _scan_range(chunk)
            if found is not None:
                return found
        return None

    methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork") if "fork" in methods else multiprocessing
    with context.Pool(processes=worker_count) as pool:
        for found in pool.imap_unordered(_scan_range, ranges):
            if stopping():
                pool.terminate()
                return None
            if found is not None:
                pool.terminate()
                return found
    return None


# ------------------------------------------------------------------- resolution


Verifier = Callable[[int, str], bool]


def _default_verifier(roots) -> Verifier:
    def verify(user_id: int, uuid: str) -> bool:
        return cipher.probe(user_id, uuid, roots) is not None

    return verify


def _cheap_candidates(
    settings: Settings,
    uuid: str,
    *,
    plists: tuple[Path, ...] | None,
    external_cache: Path | None,
) -> list[tuple[int, str, str]]:
    """``(user_id, uuid, source)`` pairs in priority order, cheap sources only."""

    ordered: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str]] = set()

    def add(user_id: int, candidate_uuid: str, source: str) -> None:
        if user_id <= 0:
            return
        key = (user_id, candidate_uuid)
        if key in seen:
            return
        seen.add(key)
        ordered.append((user_id, candidate_uuid, source))

    if settings.user_id_override is not None:
        add(settings.user_id_override, uuid, "env")

    cached = read_auth_cache(settings.kakaotalk_auth_cache_path)
    if cached is not None:
        add(cached.user_id, cached.uuid, "cache")

    for user_id in read_external_user_ids(external_cache):
        add(user_id, uuid, "external-cache")

    for user_id in plist_util.user_id_candidates(plists):
        add(user_id, uuid, "plist")

    return ordered


def _reverse_lookup_candidate(
    settings: Settings,
    uuid: str,
    *,
    plists: tuple[Path, ...] | None,
    should_stop: Callable[[], bool] | None,
) -> tuple[int, str, str] | None:
    """The expensive fallback, attempted only after everything else failed."""

    targets = set(plist_util.active_account_hashes(plists)) - plist_util.empty_account_hashes()
    if not targets:
        return None
    logger.info("starting SHA-512 reverse lookup over %d account hash(es)", len(targets))
    found = reverse_lookup(targets, should_stop=should_stop)
    return None if found is None else (found, uuid, "reverse-lookup")


def resolve_identity(
    settings: Settings,
    *,
    uuid_override: str | None = None,
    roots=None,
    plists: tuple[Path, ...] | None = None,
    external_cache: Path | None = None,
    verifier: Verifier | None = None,
    allow_reverse_lookup: bool = False,
    cache: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> AccountIdentity:
    """Find the account that owns the local database, or raise ``KakaoAuthError``."""

    uuid = uuid_override if uuid_override is not None else device_uuid()
    validate_uuid(uuid)
    verify = verifier if verifier is not None else _default_verifier(roots)

    def accept(user_id: int, candidate_uuid: str, source: str) -> AccountIdentity:
        identity = AccountIdentity(user_id=user_id, uuid=candidate_uuid, source=source)
        if cache:
            try:
                write_auth_cache(settings.kakaotalk_auth_cache_path, identity)
            except OSError as exc:  # pragma: no cover - filesystem dependent
                logger.warning("could not persist the auth cache: %s", exc)
        logger.info("resolved KakaoTalk account via %s", source)
        return identity

    for user_id, candidate_uuid, source in _cheap_candidates(
        settings, uuid, plists=plists, external_cache=external_cache
    ):
        if should_stop is not None and should_stop():
            raise KakaoAuthError("startup was interrupted before the account was resolved")
        if verify(user_id, candidate_uuid):
            return accept(user_id, candidate_uuid, source)

    if allow_reverse_lookup and settings.user_id_override is None:
        if should_stop is None or not should_stop():
            candidate = _reverse_lookup_candidate(
                settings, uuid, plists=plists, should_stop=should_stop
            )
            if candidate is not None and verify(candidate[0], candidate[1]):
                return accept(*candidate)

    raise KakaoAuthError(
        "could not determine the KakaoTalk account. Set SESAMETOK_USER_ID to the "
        "account's user id and retry."
    )


def database_path_for(identity: AccountIdentity, roots=None) -> Path | None:
    database = find_database(identity.user_id, identity.uuid, roots)
    return None if database is None else database.path


def identity_summary(identity: AccountIdentity) -> dict[str, object]:
    """Log-safe summary: no identifiers, only the source and key shape."""

    return {
        "source": identity.source,
        "user_id_digits": len(str(identity.user_id)),
        "uuid_shape": f"{len(identity.uuid)}-chars",
        "secure_key_chars": len(secure_key(identity.user_id, identity.uuid)),
    }
