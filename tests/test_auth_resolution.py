from __future__ import annotations

import hashlib
import json
import os
import plistlib
from pathlib import Path

import pytest

from sesametok_backend.config import Settings
from sesametok_backend.kakao.auth import (
    AccountIdentity,
    identity_summary,
    read_auth_cache,
    read_external_user_ids,
    resolve_identity,
    reverse_lookup,
    write_auth_cache,
)
from sesametok_backend.kakao.errors import KakaoAuthError
from support import SYNTHETIC_UUID


def _settings(tmp_path: Path, **env: str) -> Settings:
    base = {"SESAMETOK_DATA_DIR": str(tmp_path / "data")}
    base.update(env)
    return Settings.from_env(base)


def _nonexistent(tmp_path: Path) -> Path:
    return tmp_path / "no-external-cache.json"


def test_env_override_resolves_and_caches(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SESAMETOK_USER_ID="777")

    identity = resolve_identity(
        settings,
        uuid_override=SYNTHETIC_UUID,
        verifier=lambda user_id, _uuid: user_id == 777,
        plists=(),
        external_cache=_nonexistent(tmp_path),
    )

    assert (identity.user_id, identity.uuid, identity.source) == (777, SYNTHETIC_UUID, "env")
    cached = read_auth_cache(settings.kakaotalk_auth_cache_path)
    assert cached is not None
    assert (cached.user_id, cached.uuid) == (777, SYNTHETIC_UUID)


def test_cache_is_used_when_no_override(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    write_auth_cache(
        settings.kakaotalk_auth_cache_path,
        AccountIdentity(555, SYNTHETIC_UUID, "cache"),
    )

    identity = resolve_identity(
        settings,
        uuid_override=SYNTHETIC_UUID,
        verifier=lambda user_id, _uuid: user_id == 555,
        plists=(),
        external_cache=_nonexistent(tmp_path),
    )

    assert identity.source == "cache"
    assert identity.user_id == 555


def test_external_cache_is_tried_before_plists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    external = tmp_path / "k-skill.json"
    external.write_text(json.dumps({"user_id": 333}), encoding="utf-8")

    identity = resolve_identity(
        settings,
        uuid_override=SYNTHETIC_UUID,
        verifier=lambda user_id, _uuid: user_id == 333,
        plists=(),
        external_cache=external,
    )

    assert identity.source == "external-cache"
    assert identity.user_id == 333


def test_plist_candidates_are_tried_in_ascending_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plist = tmp_path / "com.kakao.KakaoTalkMac.plist"
    with plist.open("wb") as handle:
        plistlib.dump({"userId": 111, "AlertKakaoIDsList": [222]}, handle)

    tried: list[int] = []

    def verifier(user_id: int, _uuid: str) -> bool:
        tried.append(user_id)
        return user_id == 222

    identity = resolve_identity(
        settings,
        uuid_override=SYNTHETIC_UUID,
        verifier=verifier,
        plists=(plist,),
        external_cache=_nonexistent(tmp_path),
        cache=False,
    )

    assert identity.source == "plist"
    assert identity.user_id == 222
    assert tried == [111, 222]


def test_no_candidate_raises_a_helpful_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(KakaoAuthError) as excinfo:
        resolve_identity(
            settings,
            uuid_override=SYNTHETIC_UUID,
            verifier=lambda user_id, _uuid: False,
            plists=(),
            external_cache=_nonexistent(tmp_path),
        )

    assert "SESAMETOK_USER_ID" in str(excinfo.value)


def test_cache_file_is_private_and_has_no_key(tmp_path: Path) -> None:
    path = tmp_path / "auth" / "kakao.json"
    write_auth_cache(path, AccountIdentity(99, SYNTHETIC_UUID, "env"))

    assert os.stat(path).st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "user_id": 99, "uuid": SYNTHETIC_UUID}
    assert "key" not in payload


@pytest.mark.parametrize(
    "payload",
    ['not json', '[]', '{"user_id": -1, "uuid": "x"}', '{"user_id": 1}', '{"user_id": 1, "uuid": "short"}'],
)
def test_malformed_cache_is_ignored(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "kakao.json"
    path.write_text(payload, encoding="utf-8")
    assert read_auth_cache(path) is None


def test_missing_cache_is_none(tmp_path: Path) -> None:
    assert read_auth_cache(tmp_path / "absent.json") is None


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"user_id": 42}', [42]),
        ('{"user_id": "42"}', [42]),
        ('{"user_id": 0}', []),
        ('{"user_id": -3}', []),
        ('{"user_id": true}', []),
        ("not json", []),
        ('{"other": 1}', []),
    ],
)
def test_external_cache_parsing(tmp_path: Path, payload: str, expected: list[int]) -> None:
    path = tmp_path / "external.json"
    path.write_text(payload, encoding="utf-8")
    assert read_external_user_ids(path) == expected


def test_reverse_lookup_finds_a_small_user_id() -> None:
    target = hashlib.sha512(b"12345").hexdigest()
    assert reverse_lookup({target}, limit=20_000, workers=1) == 12345


def test_reverse_lookup_returns_none_when_absent() -> None:
    target = hashlib.sha512(b"999999999").hexdigest()
    assert reverse_lookup({target}, limit=20_000, workers=1) is None


def test_reverse_lookup_without_targets() -> None:
    assert reverse_lookup(set(), limit=10, workers=1) is None


def test_identity_summary_never_leaks_the_user_id() -> None:
    summary = identity_summary(AccountIdentity(1000000001, SYNTHETIC_UUID, "plist"))

    assert "1000000001" not in str(summary)
    assert summary["source"] == "plist"


def test_reverse_lookup_parallel_returns_without_hanging() -> None:
    """병렬 경로가 결과를 찾고 워커를 정리한 뒤 즉시 돌아와야 한다.

    워커가 부모의 SIGTERM 핸들러를 상속하면 pool.terminate()가 먹히지 않아
    join()이 영원히 멈춘다(실측으로 발견한 결함).
    """

    import time

    target = hashlib.sha512(b"4242").hexdigest()
    started = time.monotonic()
    found = reverse_lookup({target}, limit=200_000, workers=2)
    elapsed = time.monotonic() - started

    assert found == 4242
    assert elapsed < 30, f"reverse_lookup이 {elapsed:.1f}s 걸렸다(정리 실패 의심)"
