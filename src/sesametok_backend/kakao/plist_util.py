"""카카오톡 환경설정 plist: user id 및 활성 계정 후보.

plist의 구조만 읽는다. 값은 검증을 위해 호출자에게 반환하며, 로그에 남기거나
평문으로 저장하지 않는다.
"""

from __future__ import annotations

import hashlib
import plistlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .store import PREFERENCES_DIRS, PREFERENCES_PREFIX

USER_ID_KEYS = ("userId", "user_id", "KAKAO_USER_ID", "userID")
ALERT_IDS_KEY = "AlertKakaoIDsList"
REVISION_PREFIX = "DESIGNATEDFRIENDSREVISION:"

_HEX128 = re.compile(r"^[0-9a-f]{128}$")


def preference_plists(
    directories: Iterable[Path] | None = None,
) -> tuple[Path, ...]:
    """환경설정 디렉터리 아래의 ``com.kakao.KakaoTalkMac*.plist`` 파일들을 정렬한 것."""

    found: set[Path] = set()
    for directory in directories if directories is not None else PREFERENCES_DIRS:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if (
                entry.is_file()
                and entry.name.startswith(PREFERENCES_PREFIX)
                and entry.suffix == ".plist"
            ):
                found.add(entry)
    return tuple(sorted(found))


def load_plist(path: Path) -> dict[str, Any]:
    """plist(바이너리 또는 XML)를 파싱한다. 잘못된 파일은 빈 매핑을 돌려준다."""

    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def user_id_candidates(plists: Iterable[Path] | None = None) -> list[int]:
    """직접 키와 ``AlertKakaoIDsList`` 에서 얻은 양수 user id 후보들."""

    candidates: set[int] = set()
    for path in plists if plists is not None else preference_plists():
        data = load_plist(path)
        for key in USER_ID_KEYS:
            parsed = _positive_int(data.get(key))
            if parsed is not None:
                candidates.add(parsed)
        alert_ids = data.get(ALERT_IDS_KEY)
        if isinstance(alert_ids, (list, tuple)):
            for item in alert_ids:
                parsed = _positive_int(item)
                if parsed is not None:
                    candidates.add(parsed)
    return sorted(candidates)


def _is_active_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip() not in ("", "0", "false", "False")
    return False


def active_account_hashes(plists: Iterable[Path] | None = None) -> list[str]:
    """``DESIGNATEDFRIENDSREVISION:<hash>`` 키에서 얻은 128-hex 활성 계정 해시들."""

    hashes: set[str] = set()
    for path in plists if plists is not None else preference_plists():
        data = load_plist(path)
        for key, value in data.items():
            if not isinstance(key, str) or not key.startswith(REVISION_PREFIX):
                continue
            if not _is_active_flag(value):
                continue
            suffix = key[len(REVISION_PREFIX) :].strip().lower()
            if _HEX128.match(suffix):
                hashes.add(suffix)
    return sorted(hashes)


def empty_account_hashes() -> frozenset[str]:
    """계정 없음(no account)을 나타내는 해시들.

    *빈 계정 해시* 센티널은 제외해야 한다. 명백한 후보들을 여기 나열하고 그
    집합을 명시적으로 유지한다.
    """

    return frozenset(
        {
            hashlib.sha512(b"").hexdigest(),
            hashlib.sha512(b"0").hexdigest(),
        }
    )


def account_hash_for(user_id: int) -> str:
    """10진수 user id의 SHA-512. plist 키와 비교하는 값이다."""

    return hashlib.sha512(str(user_id).encode("ascii")).hexdigest()


def plist_report(plists: Iterable[Path] | None = None) -> dict[str, int]:
    """개수만 담는다 — 식별자 자체는 절대 담지 않는다."""

    paths = tuple(plists if plists is not None else preference_plists())
    return {
        "plists": len(paths),
        "user_id_candidates": len(user_id_candidates(paths)),
        "active_account_hashes": len(active_account_hashes(paths)),
    }
