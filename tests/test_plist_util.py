from __future__ import annotations

import hashlib
import plistlib
from pathlib import Path

from sesametok_backend.kakao import plist_util


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)
    return path


def test_user_id_candidates_collect_and_sort(tmp_path: Path) -> None:
    first = _write(
        tmp_path / "com.kakao.KakaoTalkMac.plist",
        {"userId": 300, "AlertKakaoIDsList": [100, "200", 0, -5]},
    )
    second = _write(tmp_path / "com.kakao.KakaoTalkMac.other.plist", {"user_id": 200})

    assert plist_util.user_id_candidates((first, second)) == [100, 200, 300]


def test_user_id_candidates_ignore_booleans_and_bad_values(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "com.kakao.KakaoTalkMac.plist",
        {"userId": True, "userID": "not-a-number", "AlertKakaoIDsList": "nope"},
    )
    assert plist_util.user_id_candidates((path,)) == []


def test_active_account_hashes_only_accepts_non_zero_128_hex(tmp_path: Path) -> None:
    active = hashlib.sha512(b"1").hexdigest()
    inactive = hashlib.sha512(b"2").hexdigest()
    path = _write(
        tmp_path / "com.kakao.KakaoTalkMac.plist",
        {
            f"DESIGNATEDFRIENDSREVISION:{active}": 1,
            f"DESIGNATEDFRIENDSREVISION:{inactive}": 0,
            f"DESIGNATEDFRIENDSREVISION:{'f' * 128}": True,
            "DESIGNATEDFRIENDSREVISION:short": 1,
        },
    )

    assert plist_util.active_account_hashes((path,)) == sorted({active, "f" * 128})


def test_malformed_plist_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "com.kakao.KakaoTalkMac.plist"
    path.write_bytes(b"this is not a plist")

    assert plist_util.load_plist(path) == {}
    assert plist_util.user_id_candidates((path,)) == []


def test_account_hash_for_matches_sha512(tmp_path: Path) -> None:
    assert plist_util.account_hash_for(7) == hashlib.sha512(b"7").hexdigest()


def test_empty_account_hashes_are_the_obvious_sentinels() -> None:
    sentinels = plist_util.empty_account_hashes()
    assert hashlib.sha512(b"").hexdigest() in sentinels
    assert hashlib.sha512(b"0").hexdigest() in sentinels


def test_plist_report_returns_counts_only(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "com.kakao.KakaoTalkMac.plist",
        {"userId": 42, f"DESIGNATEDFRIENDSREVISION:{hashlib.sha512(b'42').hexdigest()}": 1},
    )

    report = plist_util.plist_report((path,))

    assert report == {"plists": 1, "user_id_candidates": 1, "active_account_hashes": 1}
    assert "42" not in str(report)


def test_preference_plists_filters_by_prefix_and_suffix(tmp_path: Path) -> None:
    wanted = _write(tmp_path / "com.kakao.KakaoTalkMac.plist", {})
    _write(tmp_path / "com.other.App.plist", {})
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    assert plist_util.preference_plists((tmp_path,)) == (wanted,)
