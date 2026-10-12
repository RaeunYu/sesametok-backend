"""로컬 데이터베이스를 소유한 ``(user_id, uuid)`` 를 찾아낸다.

후보는 우선순위 순서로 시도하며, 각 후보는 데이터베이스 잠금을 해제하여 스스로를
*증명*해야 한다. 선택된 조합은 캐시하지만 패스프레이즈는 절대 캐시하지 않는다.
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


# ---------------------------------------------------------------------- 캐싱


def read_auth_cache(path: Path) -> AccountIdentity | None:
    """``{"user_id": ..., "uuid": ...}`` 를 읽는다. 잘못된 형식의 캐시는 무시한다."""

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
    """신원을 0600 권한으로 원자적 이름 바꾸기를 통해 저장한다. 키는 절대 저장하지 않는다."""

    payload = {"version": AUTH_CACHE_VERSION, "user_id": identity.user_id, "uuid": identity.uuid}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False) + "\n")


def read_external_user_ids(path: Path | None = None) -> list[int]:
    """k-skill 캐시 파일이 존재한다면 그곳의 user id들."""

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


# ------------------------------------------------------------- 역방향 조회


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
    """``sha512(str(user_id))`` 가 *target_hashes* 에 있는 user id를 무차별 대입한다.

    구조적으로 비싼 작업이며, 더 저렴한 모든 경로가 실패했을 때만 도달한다.
    ``fork`` 를 사용할 수 있으면 그것을 선호하여 워커가 진입점을 다시 임포트하지
    않게 한다. 이는 워커 스레드가 존재하기 전인 시작 시점에 실행된다.
    ``should_stop`` 콜백을 통해 시그널이 탐색을 즉시 중단할 수 있다.
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


# ------------------------------------------------------------------- 해석


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
    """우선순위 순서의 ``(user_id, uuid, source)`` 쌍. 저렴한 소스만 사용한다."""

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
    """비싼 대체 경로로, 다른 모든 방법이 실패한 뒤에만 시도한다."""

    targets = set(plist_util.active_account_hashes(plists)) - plist_util.empty_account_hashes()
    if not targets:
        return None
    logger.info("%d개의 계정 해시에 대해 SHA-512 역방향 조회를 시작한다", len(targets))
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
    """로컬 데이터베이스를 소유한 계정을 찾는다. 실패하면 ``KakaoAuthError`` 를 발생시킨다."""

    uuid = uuid_override if uuid_override is not None else device_uuid()
    validate_uuid(uuid)
    verify = verifier if verifier is not None else _default_verifier(roots)

    def accept(user_id: int, candidate_uuid: str, source: str) -> AccountIdentity:
        identity = AccountIdentity(user_id=user_id, uuid=candidate_uuid, source=source)
        if cache:
            try:
                write_auth_cache(settings.kakaotalk_auth_cache_path, identity)
            except OSError as exc:  # pragma: no cover - 파일시스템에 따라 달라짐
                logger.warning("인증 캐시를 저장할 수 없다: %s", exc)
        logger.info("%s 경로로 카카오톡 계정을 확인했다", source)
        return identity

    for user_id, candidate_uuid, source in _cheap_candidates(
        settings, uuid, plists=plists, external_cache=external_cache
    ):
        if should_stop is not None and should_stop():
            raise KakaoAuthError("계정을 확인하기 전에 시작이 중단되었다")
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
        "카카오톡 계정을 확인할 수 없다. SESAMETOK_USER_ID를 계정의 user id로 "
        "설정한 뒤 다시 시도하라."
    )


def database_path_for(identity: AccountIdentity, roots=None) -> Path | None:
    database = find_database(identity.user_id, identity.uuid, roots)
    return None if database is None else database.path


def identity_summary(identity: AccountIdentity) -> dict[str, object]:
    """로그에 안전한 요약: 식별자 없이 소스와 키 형태만 담는다."""

    return {
        "source": identity.source,
        "user_id_digits": len(str(identity.user_id)),
        "uuid_shape": f"{len(identity.uuid)}-chars",
        "secure_key_chars": len(secure_key(identity.user_id, identity.uuid)),
    }
