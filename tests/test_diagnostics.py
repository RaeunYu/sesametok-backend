from __future__ import annotations

from pathlib import Path

from sesametok_backend import diagnostics
from sesametok_backend.config import Settings


def test_accessibility_check_returns_bool_or_none() -> None:
    assert diagnostics.accessibility_trusted() in (True, False, None)


def test_store_root_report_marks_missing_and_readable(tmp_path: Path) -> None:
    readable = tmp_path / "present"
    readable.mkdir()
    report = diagnostics.store_root_report((readable, tmp_path / "missing"))

    assert report[0]["exists"] is True
    assert report[0]["readable"] is True
    assert report[1]["exists"] is False
    assert report[1]["readable"] is False


def test_collect_shape(tmp_path: Path) -> None:
    settings = Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)})
    report = diagnostics.collect(settings, (tmp_path / "missing",))

    assert set(report) == {
        "python_executable",
        "invoked_executable",
        "python_is_symlinked",
        "grant_target",
        "grant_target_is_app",
        "kakaotalk_installed",
        "kakaotalk_running",
        "accessibility_trusted",
        "store_readable",
        "full_disk_access",
        "store_roots",
        "menubar",
    }
    assert Path(report["python_executable"]).is_absolute()
    # 저장소 루트가 없으므로 읽을 수 없고, FDA는 별도로 판정된다.
    assert report["store_readable"] is False
    assert report["full_disk_access"] in (True, False, None)


def test_guidance_names_the_executable_to_grant(tmp_path: Path) -> None:
    report = diagnostics.collect(
        Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)}), (tmp_path / "missing",)
    )
    report["accessibility_trusted"] = False
    lines = diagnostics.permission_guidance(report)
    joined = "\n".join(lines)

    assert report["grant_target"] in joined
    assert "전체 디스크 접근" in joined
    assert "손쉬운 사용" in joined


def test_no_guidance_when_everything_is_fine(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    report = diagnostics.collect(
        Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)}), (root,)
    )
    report.update(
        {
            "accessibility_trusted": True,
            "kakaotalk_installed": True,
            "kakaotalk_running": True,
            "store_readable": True,
            "python_is_symlinked": False,
        }
    )
    assert diagnostics.permission_guidance(report) == []


def test_symlinked_interpreter_is_called_out(tmp_path: Path) -> None:
    # 앱이 없을 때의 경로를 검증한다(앱이 있으면 권한 대상이 앱이라 경고가 사라진다).
    report = diagnostics.collect(
        Settings.from_env(
            {
                "SESAMETOK_DATA_DIR": str(tmp_path),
                "SESAMETOK_APP_BUNDLE": str(tmp_path / "Missing.app"),
            }
        ),
        (tmp_path / "missing",),
    )
    report.update(
        {
            "accessibility_trusted": True,
            "kakaotalk_installed": True,
            "kakaotalk_running": True,
            "store_readable": True,
            "python_is_symlinked": True,
            "invoked_executable": "/venv/bin/python",
            "python_executable": "/real/3.13/bin/python3.13",
        }
    )
    joined = "\n".join(diagnostics.permission_guidance(report))
    assert "심볼릭 링크" in joined
    assert "권한이 무효" in joined


def test_guidance_mentions_kakaotalk_not_running(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    report = diagnostics.collect(
        Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)}), (root,)
    )
    report.update({"accessibility_trusted": True, "kakaotalk_installed": True,
                   "kakaotalk_running": False, "store_readable": True})
    assert any("실행 중이 아닙니다" in line for line in diagnostics.permission_guidance(report))


def test_fda_probe_is_independent_of_the_kakao_store(tmp_path: Path) -> None:
    """저장소가 읽힌다고 해서 전체 디스크 접근 권한이 부여된 것은 아니다."""

    root = tmp_path / "store"
    root.mkdir()
    report = diagnostics.collect(
        Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)}), (root,)
    )

    assert report["store_readable"] is True
    assert report["full_disk_access"] == diagnostics.full_disk_access_granted()
    # 판정 대상은 TCC 보호 경로이며 카카오톡 컨테이너가 아니다.
    assert all("KakaoTalk" not in str(path) for path in diagnostics.FDA_PROBE_PATHS)
