from __future__ import annotations

import json

import pytest

from sesametok_backend.cli import EXIT_CONFIG, EXIT_UNAVAILABLE, build_parser, main


def test_parser_has_the_documented_commands() -> None:
    parser = build_parser()
    actions = [
        action
        for action in parser._subparsers._actions  # type: ignore[union-attr]
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    commands = set(actions[0].choices)
    assert {
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "token",
        "foreground",
        "install",
        "uninstall",
        "permissions",
        "archive-key",
    } <= commands


def test_status_json_reports_stopped(env, capsys) -> None:
    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is False
    assert payload["config"]["data_dir"].endswith("data")
    # 토큰은 항상 요약만 되고, 절대 출력되지 않는다.
    assert payload["config"]["api_token"] in {"set", "unset"}


def test_token_command_creates_and_prints(env, capsys) -> None:
    assert main(["token"]) == 0
    printed = capsys.readouterr().out.strip()
    assert len(printed) >= 32


def test_lifecycle_commands_are_wired() -> None:
    """M5에서 대기 스텁을 걷어냈다: 모든 수명주기 명령에 핸들러가 있다."""

    parser = build_parser()
    actions = [
        action
        for action in parser._subparsers._actions  # type: ignore[union-attr]
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    choices = actions[0].choices
    for command in ("start", "stop", "restart", "install", "uninstall"):
        assert choices[command].get_default("handler") is not None


def test_stop_when_not_installed_is_a_clean_noop(env, capsys) -> None:
    # env 픽스처가 plist를 tmp_path로 돌리므로 실제 경로는 건드리지 않는다.
    assert main(["stop"]) == 0
    captured = capsys.readouterr()
    assert "실행 중이 아닙니다" in captured.out
    assert "등록된 핸들러가 없습니다" not in captured.err


def test_archive_key_refuses_non_interactive_without_force(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_ARCHIVE_KEY", "ab" * 32)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert main(["archive-key"]) == EXIT_UNAVAILABLE
    assert "출력하지 않습니다" in capsys.readouterr().err


def test_archive_key_force_prints_the_key(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_ARCHIVE_KEY", "ab" * 32)

    assert main(["archive-key", "--force"]) == 0
    assert capsys.readouterr().out.strip() == "ab" * 32


def test_archive_key_reports_a_bad_key(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_ARCHIVE_KEY", "not-a-key")
    assert main(["archive-key", "--force"]) == 1
    # 오류 문구 자체는 security/archive_key.py가 만든다(이 스위트의 소유가 아니다);
    # CLI가 자기 접두사와 함께 무언가를 보고했는지만 확인한다.
    err = capsys.readouterr().err
    assert err.strip()
    assert "sesametok-backend:" in err


def test_logs_without_file_fails(env, capsys) -> None:
    assert main(["logs"]) == 1
    assert "로그 파일이 없습니다" in capsys.readouterr().err


def test_configuration_error_is_reported(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_PORT", "not-a-port")
    assert main(["status"]) == EXIT_CONFIG
    assert "설정 오류" in capsys.readouterr().err


def test_version_flag_exits() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
