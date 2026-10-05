"""KakaoTalk preference plists: user-id and active-account candidates.

Only the structure of the plist is read. Values are returned to the caller for
verification and are never logged or persisted in plaintext.
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
    """Sorted ``com.kakao.KakaoTalkMac*.plist`` files under the preference dirs."""

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
    """Parse a plist (binary or XML). Malformed files yield an empty mapping."""

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
    """Positive user-id candidates from direct keys and ``AlertKakaoIDsList``."""

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
    """128-hex active-account hashes from ``DESIGNATEDFRIENDSREVISION:<hash>`` keys."""

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
    """Hashes that stand for "no account".

    An *empty account hash* sentinel must be excluded; the obvious candidates are
    listed here and the set is kept explicit.
    """

    return frozenset(
        {
            hashlib.sha512(b"").hexdigest(),
            hashlib.sha512(b"0").hexdigest(),
        }
    )


def account_hash_for(user_id: int) -> str:
    """SHA-512 of the decimal user id, the value compared against the plist keys."""

    return hashlib.sha512(str(user_id).encode("ascii")).hexdigest()


def plist_report(plists: Iterable[Path] | None = None) -> dict[str, int]:
    """Counts only — never the identifiers themselves."""

    paths = tuple(plists if plists is not None else preference_plists())
    return {
        "plists": len(paths),
        "user_id_candidates": len(user_id_candidates(paths)),
        "active_account_hashes": len(active_account_hashes(paths)),
    }
