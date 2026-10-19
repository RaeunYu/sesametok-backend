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
        "register",
        "unregister",
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
    for command in ("start", "stop", "restart", "register", "unregister"):
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


# --------------------------------------------------- 권한 출처 · 고아 프로세스 복구


class _AgentStub:
    """cmd_start/cmd_stop이 쓰는 LaunchAgent의 최소 대역."""

    def __init__(self, path, *, running: bool = False, loaded: bool = False) -> None:
        self._path = path
        self._running = running
        self._loaded = loaded
        self.bootstrapped = False
        self.booted_out = False

    @property
    def plist_path(self):
        return self._path

    def is_running(self) -> bool:
        return self._running

    def is_loaded(self) -> bool:
        return self._loaded

    def install_plist(self):
        return self.plist_path

    def bootstrap(self):
        from types import SimpleNamespace

        self.bootstrapped = True
        return SimpleNamespace(ok=True, message="")

    def kickstart(self, *, restart: bool = True):
        from types import SimpleNamespace

        return SimpleNamespace(ok=True, message="")

    def bootout(self):
        from types import SimpleNamespace

        self.booted_out = True
        return SimpleNamespace(ok=True, message="")

    def status(self):
        from types import SimpleNamespace

        return SimpleNamespace(pid=4321, loaded=self._loaded, running=self._running, state="running")


def test_status_prefers_the_running_backend_view(env, monkeypatch, capsys) -> None:
    import sesametok_backend.cli as cli

    monkeypatch.setattr(cli, "running_pid", lambda settings: 4242)
    monkeypatch.setattr(
        cli,
        "_backend_permissions",
        lambda settings: {
            "accessibility_trusted": True,
            "store_readable": True,
            "full_disk_access": False,
            "grant_target": "/Applications/SesametokBackend.app",
            "grant_target_is_app": True,
        },
    )

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "정상" in out
    assert "권한 측정: 백엔드" in out


def test_status_falls_back_to_the_terminal_view(env, monkeypatch, capsys) -> None:
    import sesametok_backend.cli as cli

    monkeypatch.setattr(cli, "running_pid", lambda settings: None)

    assert main(["status"]) == 0
    assert "권한 측정: 터미널" in capsys.readouterr().out


def test_stop_cleans_up_an_orphaned_child(env, monkeypatch, capsys) -> None:
    """bootout은 앱 스텁만 정지시킨다. 자식이 남으면 stop이 그것까지 정리해야 한다."""

    import sesametok_backend.cli as cli

    agent = _AgentStub(env / "agent.plist", running=True, loaded=True)
    agent.plist_path.write_text("<plist/>", encoding="utf-8")
    terminated: list[int] = []
    monkeypatch.setattr(cli, "_launch_agent", lambda settings: agent)
    monkeypatch.setattr(cli, "running_pid", lambda settings: 9999)
    monkeypatch.setattr(cli, "wait_until_stopped", lambda agent, timeout: True)
    monkeypatch.setattr(cli, "terminate_process", lambda pid, timeout=5.0: terminated.append(pid) or True)

    assert main(["stop"]) == 0
    assert agent.booted_out is True
    assert terminated == [9999]


def test_start_recovers_from_an_orphaned_child(env, monkeypatch, capsys) -> None:
    """launchd가 언로드됐는데 락만 잡고 있는 고아를 정리하고 기동해야 한다."""

    import sesametok_backend.cli as cli

    agent = _AgentStub(env / "agent.plist", running=False, loaded=False)
    terminated: list[int] = []
    monkeypatch.setattr(cli, "_launch_agent", lambda settings: agent)
    monkeypatch.setattr(cli, "running_pid", lambda settings: 9999)
    monkeypatch.setattr(cli, "terminate_process", lambda pid, timeout=5.0: terminated.append(pid) or True)
    monkeypatch.setattr(cli, "wait_until_running", lambda agent, timeout: True)

    assert main(["start"]) == 0
    assert terminated == [9999]
    assert agent.bootstrapped is True
