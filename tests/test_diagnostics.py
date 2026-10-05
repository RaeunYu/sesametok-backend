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
        "kakaotalk_installed",
        "kakaotalk_running",
        "accessibility_trusted",
        "full_disk_access_ok",
        "store_roots",
        "menubar",
    }
    assert Path(report["python_executable"]).is_absolute()
    assert report["full_disk_access_ok"] is False


def test_guidance_names_the_executable_to_grant(tmp_path: Path) -> None:
    report = diagnostics.collect(
        Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)}), (tmp_path / "missing",)
    )
    report["accessibility_trusted"] = False
    lines = diagnostics.permission_guidance(report)
    joined = "\n".join(lines)

    assert report["python_executable"] in joined
    assert "Full Disk Access" in joined
    assert "Accessibility" in joined


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
            "full_disk_access_ok": True,
            "python_is_symlinked": False,
        }
    )
    assert diagnostics.permission_guidance(report) == []


def test_symlinked_interpreter_is_called_out(tmp_path: Path) -> None:
    report = diagnostics.collect(
        Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)}), (tmp_path / "missing",)
    )
    report.update(
        {
            "accessibility_trusted": True,
            "kakaotalk_installed": True,
            "kakaotalk_running": True,
            "full_disk_access_ok": True,
            "python_is_symlinked": True,
            "invoked_executable": "/venv/bin/python",
            "python_executable": "/real/3.13/bin/python3.13",
        }
    )
    joined = "\n".join(diagnostics.permission_guidance(report))
    assert "symlink" in joined
    assert "invalidates the grant" in joined


def test_guidance_mentions_kakaotalk_not_running(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    report = diagnostics.collect(
        Settings.from_env({"SESAMETOK_DATA_DIR": str(tmp_path)}), (root,)
    )
    report.update({"accessibility_trusted": True, "kakaotalk_installed": True,
                   "kakaotalk_running": False, "full_disk_access_ok": True})
    assert any("not running" in line for line in diagnostics.permission_guidance(report))
