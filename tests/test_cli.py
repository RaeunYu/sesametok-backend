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
    # The token is only ever summarised, never printed.
    assert payload["config"]["api_token"] in {"set", "unset"}


def test_token_command_creates_and_prints(env, capsys) -> None:
    assert main(["token"]) == 0
    printed = capsys.readouterr().out.strip()
    assert len(printed) >= 32


def test_lifecycle_commands_are_wired() -> None:
    """M5 replaced the pending stubs: every lifecycle command has a handler."""

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
    # The env fixture points the plist at tmp_path, so nothing real is touched.
    assert main(["stop"]) == 0
    captured = capsys.readouterr()
    assert "not running" in captured.out
    assert "not wired up yet" not in captured.err


def test_archive_key_refuses_non_interactive_without_force(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_ARCHIVE_KEY", "ab" * 32)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert main(["archive-key"]) == EXIT_UNAVAILABLE
    assert "refusing to print" in capsys.readouterr().err


def test_archive_key_force_prints_the_key(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_ARCHIVE_KEY", "ab" * 32)

    assert main(["archive-key", "--force"]) == 0
    assert capsys.readouterr().out.strip() == "ab" * 32


def test_archive_key_reports_a_bad_key(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_ARCHIVE_KEY", "not-a-key")
    assert main(["archive-key", "--force"]) == 1
    assert "archive key" in capsys.readouterr().err


def test_logs_without_file_fails(env, capsys) -> None:
    assert main(["logs"]) == 1
    assert "no log file" in capsys.readouterr().err


def test_configuration_error_is_reported(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SESAMETOK_PORT", "not-a-port")
    assert main(["status"]) == EXIT_CONFIG
    assert "configuration error" in capsys.readouterr().err


def test_version_flag_exits() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
