"""런처 테스트: plist 생성, launchctl 래퍼, CLI 수명주기.

여기서는 실제 ``launchctl``을 실행하거나 임시 디렉터리 밖에 쓰지 않는다.
``FakeLaunchctl``은 러너 이음매를 대신하는 메모리 내 대체물이며, 모든 plist 경로는
``env`` 픽스처(또는 *tmp_path*)에서 온다.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from sesametok_backend import cli, diagnostics, launcher
from sesametok_backend.config import LAUNCHD_LABEL, Settings
from sesametok_backend.launcher import (
    LaunchAgent,
    LaunchctlResult,
    parse_launchctl_print,
    render_plist,
)

RUNNING_PRINT = (
    f"gui/501/{LAUNCHD_LABEL} = {{\n"
    "\tactive count = 1\n"
    "\tstate = running\n"
    "\n"
    "\tprogram = /tmp/venv/bin/python\n"
    "\tpid = 4321\n"
    "}\n"
)
WAITING_PRINT = f"gui/501/{LAUNCHD_LABEL} = {{\n\tstate = waiting\n}}\n"
NOT_FOUND = f'Could not find service "{LAUNCHD_LABEL}" in domain for user gui: 501'


class FakeLaunchctl:
    """메모리 내 ``launchctl``: argv를 기록하고 로드/실행 상태를 모델링한다."""

    def __init__(
        self,
        *,
        loaded: bool = False,
        running: bool = False,
        fail_verb: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.loaded = loaded
        self.running = running
        self.fail_verb = fail_verb

    def __call__(self, argv, timeout: float) -> LaunchctlResult:
        call = [str(part) for part in argv]
        self.calls.append(call)
        verb = call[1] if len(call) > 1 else ""
        if verb == self.fail_verb:
            return LaunchctlResult(tuple(call), 1, "", "boom")
        if verb == "print":
            if not self.loaded:
                return LaunchctlResult(tuple(call), 113, "", NOT_FOUND)
            text = RUNNING_PRINT if self.running else WAITING_PRINT
            return LaunchctlResult(tuple(call), 0, text, "")
        if verb == "bootstrap":
            self.loaded, self.running = True, True
        elif verb == "bootout":
            self.loaded, self.running = False, False
        elif verb == "kickstart":
            self.running = True
        return LaunchctlResult(tuple(call), 0, "", "")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if len(call) > 1]


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


# ----------------------------------------------------------------------- plist 내용


def test_render_plist_has_the_full_contract(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    text = render_plist(settings)
    payload = plistlib.loads(text.encode("utf-8"))

    assert payload["Label"] == LAUNCHD_LABEL
    argv = payload["ProgramArguments"]
    assert argv[0] == str(settings.python_executable)
    assert Path(argv[0]).is_absolute()
    assert argv[1:] == ["-m", "sesametok_backend", "foreground", "--managed"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ProcessType"] == "Interactive"
    assert payload["LimitLoadToSessionType"] == "Aqua"
    assert payload["StandardOutPath"] == str(settings.log_dir / "out.log")
    assert payload["StandardErrorPath"] == str(settings.log_dir / "err.log")
    assert payload["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert "--managed" in text


def test_install_plist_writes_parseable_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = tmp_path / "LaunchAgents" / "agent.plist"

    written = launcher.install_plist(settings, path)

    assert written == path
    assert path.is_file()
    assert plistlib.loads(path.read_bytes())["Label"] == LAUNCHD_LABEL
    assert settings.log_dir.is_dir()


# --------------------------------------------------------------------------- 가드


def test_plist_guard_rejects_machine_paths(monkeypatch) -> None:
    monkeypatch.delenv("SESAMETOK_ALLOW_PLIST_OVERRIDE", raising=False)

    assert launcher.plist_path_is_allowed(Path("/etc/sesametok/agent.plist")) is False
    with pytest.raises(launcher.PlistPathNotAllowed):
        launcher.require_plist_path_allowed(Path("/etc/sesametok/agent.plist"))


def test_plist_guard_allows_temp_and_launch_agents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SESAMETOK_ALLOW_PLIST_OVERRIDE", raising=False)

    assert launcher.plist_path_is_allowed(tmp_path / "agent.plist") is True
    assert launcher.plist_path_is_allowed(launcher.launch_agents_dir() / "a.plist") is True


def test_plist_guard_honours_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("SESAMETOK_ALLOW_PLIST_OVERRIDE", "1")
    assert launcher.plist_path_is_allowed(Path("/opt/elsewhere/agent.plist")) is True


# ------------------------------------------------------------------ launchctl 명령


def test_launchagent_generates_the_documented_command_shapes(tmp_path: Path) -> None:
    fake = FakeLaunchctl()
    plist = tmp_path / "agent.plist"
    agent = LaunchAgent(_settings(tmp_path), plist_path=plist, runner=fake, uid=501)

    agent.bootstrap()
    agent.bootout()
    agent.kickstart()
    agent.print_state()

    assert fake.calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "bootout", f"gui/501/{LAUNCHD_LABEL}"],
        ["launchctl", "kickstart", "-k", f"gui/501/{LAUNCHD_LABEL}"],
        ["launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"],
    ]


def test_is_running_parses_running_print(tmp_path: Path) -> None:
    fake = FakeLaunchctl(loaded=True, running=True)
    agent = LaunchAgent(
        _settings(tmp_path), plist_path=tmp_path / "a.plist", runner=fake, uid=501
    )

    assert agent.is_loaded() is True
    assert agent.is_running() is True
    status = agent.status()
    assert status.pid == 4321
    assert status.state == "running"


def test_is_running_treats_not_found_as_not_loaded(tmp_path: Path) -> None:
    fake = FakeLaunchctl(loaded=False)
    agent = LaunchAgent(
        _settings(tmp_path), plist_path=tmp_path / "a.plist", runner=fake, uid=501
    )

    assert agent.is_loaded() is False
    assert agent.is_running() is False
    assert agent.status().error == NOT_FOUND


def test_loaded_but_not_running_is_not_running(tmp_path: Path) -> None:
    fake = FakeLaunchctl(loaded=True, running=False)
    agent = LaunchAgent(
        _settings(tmp_path), plist_path=tmp_path / "a.plist", runner=fake, uid=501
    )

    assert agent.is_loaded() is True
    assert agent.is_running() is False
    assert agent.status().state == "waiting"


def test_parse_launchctl_print_handles_samples() -> None:
    assert parse_launchctl_print(RUNNING_PRINT) == ("running", 4321)
    assert parse_launchctl_print(WAITING_PRINT) == ("waiting", None)
    assert parse_launchctl_print("") == (None, None)


# ----------------------------------------------------------------------- CLI 수명주기


def test_cli_install_writes_plist_and_bootstraps(env, monkeypatch, capsys) -> None:
    fake = FakeLaunchctl(loaded=False, running=False)
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["install"]) == 0

    plist = env / "agent.plist"
    assert plist.is_file()
    assert plistlib.loads(plist.read_bytes())["Label"] == LAUNCHD_LABEL
    assert "bootstrap" in fake.verbs()
    out = capsys.readouterr().out
    assert "권한 부여 대상: " + diagnostics.python_executable() in out
    assert "전체 디스크 접근 권한" in out


def test_cli_uninstall_bootouts_and_removes_plist(env, monkeypatch, capsys) -> None:
    launcher.install_plist(Settings.from_env())
    fake = FakeLaunchctl(loaded=True, running=True)
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["uninstall"]) == 0

    assert not (env / "agent.plist").exists()
    assert "bootout" in fake.verbs()
    assert "데이터는 그대로 두었습니다" in capsys.readouterr().out


def test_cli_start_is_idempotent_when_already_running(env, monkeypatch, capsys) -> None:
    started: list[str] = []

    def fake_bootstrap(self) -> LaunchctlResult:
        started.append("bootstrap")
        return LaunchctlResult(("launchctl", "bootstrap"), 0)

    monkeypatch.setattr(launcher.LaunchAgent, "is_running", lambda self: True)
    monkeypatch.setattr(launcher.LaunchAgent, "bootstrap", fake_bootstrap)

    assert cli.main(["start"]) == 0

    assert started == []
    assert "이미 실행 중입니다" in capsys.readouterr().out


def test_cli_start_bootstraps_and_waits_until_running(env, monkeypatch, capsys) -> None:
    fake = FakeLaunchctl(loaded=False, running=False)
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["start"]) == 0

    assert fake.verbs().count("bootstrap") == 1
    assert "시작되었습니다" in capsys.readouterr().out
    assert (env / "agent.plist").is_file()


def test_cli_start_reports_a_bootstrap_failure(env, monkeypatch, capsys) -> None:
    fake = FakeLaunchctl(loaded=False, running=False, fail_verb="bootstrap")
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["start"]) == 1

    assert "에이전트를 시작하지 못했습니다" in capsys.readouterr().err


def test_cli_stop_when_not_running_is_safe(env, monkeypatch, capsys) -> None:
    fake = FakeLaunchctl(loaded=False, running=False)
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["stop"]) == 0

    out = capsys.readouterr().out
    assert "실행 중이 아닙니다" in out
    assert "bootout" not in fake.verbs()


def test_cli_stop_bootouts_a_running_agent(env, monkeypatch, capsys) -> None:
    launcher.install_plist(Settings.from_env())
    fake = FakeLaunchctl(loaded=True, running=True)
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["stop"]) == 0

    assert "bootout" in fake.verbs()
    assert "중지되었습니다" in capsys.readouterr().out


def test_cli_restart_bootouts_then_bootstraps_once(env, monkeypatch, capsys) -> None:
    launcher.install_plist(Settings.from_env())
    fake = FakeLaunchctl(loaded=True, running=True)
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["restart"]) == 0

    verbs = fake.verbs()
    assert verbs.count("bootstrap") == 1
    assert verbs.index("bootout") < verbs.index("bootstrap")
    assert "재시작되었습니다" in capsys.readouterr().out


def test_cli_refuses_to_touch_a_plist_outside_the_allowed_locations(
    env, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("SESAMETOK_ALLOW_PLIST_OVERRIDE", raising=False)
    monkeypatch.setenv("SESAMETOK_LAUNCHD_PLIST", "/etc/sesametok/agent.plist")
    fake = FakeLaunchctl()
    monkeypatch.setattr(launcher, "run_launchctl", fake)

    assert cli.main(["install"]) == 1

    assert fake.calls == []
    assert "관리를 거부합니다" in capsys.readouterr().err
