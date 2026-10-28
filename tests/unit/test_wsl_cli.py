"""WSL CLI checks are static unless explicitly asked to initialize."""

from types import SimpleNamespace

import pytest

from acp_gateway.cli import doctor
from acp_gateway.config import (
    AgentProfile,
    AppConfig,
    SecretStore,
    Settings,
    omit_default_idle_timeout,
)


def configured(tmp_path, backend):
    profile = AgentProfile.model_validate(
        {
            "alias": "code",
            "kind": backend,
            "backend": backend,
            "default_cwd": "/home/alice/project-not-on-host",
            backend: {
                "executable": "/opt/agent/bin/cli",
                "wsl": {"distribution": "Ubuntu-22.04", "user": "alice"},
            },
        }
    )
    return AppConfig(Settings(agents=[profile]), SecretStore(), None, None)


@pytest.mark.parametrize("backend", ["codex", "claude_code"])
@pytest.mark.parametrize(
    "platform,launcher,status",
    [("linux", "wsl.exe", "error"), ("win32", None, "error"), ("win32", "wsl.exe", "info")],
)
def test_doctor_wsl_static_checks_target_not_host(
    tmp_path, monkeypatch, backend, platform, launcher, status
):
    cfg = configured(tmp_path, backend)
    path = tmp_path / "config.yaml"
    path.touch()
    monkeypatch.setattr(doctor, "load_config", lambda *a: cfg)
    monkeypatch.setattr(doctor, "_snapshot", lambda *a: None)
    monkeypatch.setattr(
        doctor, "owner_token", lambda *a: SimpleNamespace(get_secret_value=lambda: "owner")
    )
    monkeypatch.setattr(doctor, "sys", SimpleNamespace(platform=platform), raising=False)
    import shutil

    calls = []

    def which(name):
        calls.append(name)
        return launcher if name == "wsl.exe" else None

    monkeypatch.setattr(shutil, "which", which)
    checks = doctor.collect(path)
    rows = [c for c in checks if c["name"] == "agent:code"]
    assert len(rows) == 1 and rows[0]["status"] == status
    assert "WSL" in rows[0]["detail"]
    assert not any(c["name"] == "cwd:code" for c in checks)
    assert all(name == "wsl.exe" for name in calls)
    if status == "info":
        assert "Ubuntu-22.04" in rows[0]["detail"] and "alice" in rows[0]["detail"]
        assert "not checked" in rows[0]["detail"]


@pytest.mark.parametrize("backend", ["codex", "claude_code"])
@pytest.mark.parametrize("fails", [False, True])
def test_doctor_wsl_live_uses_owned_initialize(tmp_path, monkeypatch, backend, fails):
    cfg = configured(tmp_path, backend)
    path = tmp_path / "config.yaml"
    path.touch()
    monkeypatch.setattr(doctor, "load_config", lambda *a: cfg)
    monkeypatch.setattr(doctor, "_snapshot", lambda *a: None)
    monkeypatch.setattr(
        doctor, "owner_token", lambda *a: SimpleNamespace(get_secret_value=lambda: "owner")
    )
    monkeypatch.setattr(doctor, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr("shutil.which", lambda name: "wsl.exe" if name == "wsl.exe" else None)
    monkeypatch.setattr(
        doctor.httpx,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(doctor.httpx.ConnectError("offline")),
    )
    calls = []

    class Client:
        def __init__(self, profile, secret, **kwargs):
            assert profile is cfg.settings.agents[0] and secret is None
            assert kwargs["open_timeout"] == 2

        async def connect(self):
            calls.append("initialize")
            if fails:
                raise ValueError("PRIVATE-ERROR-CONTENTS")

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(doctor, "AgentClient", Client)
    checks = doctor.collect(path, live=True, timeout=2)
    row = next(c for c in checks if c["name"] == "agent:code")
    assert calls == ["initialize", "close"]
    assert row["status"] == ("error" if fails else "ok")
    assert "PRIVATE-ERROR-CONTENTS" not in str(checks)
    assert "WSL Ubuntu-22.04/alice" in row["detail"]


class Answers:
    def __init__(self, **answers):
        self.answers = answers
        self.labels = []

    def ask(self, label, default="", validate=None, **kwargs):
        self.labels.append(label)
        value = self.answers.get(label, default)
        return validate(value) if validate else value

    def choice(self, label, choices, default):
        self.labels.append(label)
        value = self.answers.get(label, default)
        assert value in choices
        return value

    def yes(self, label, default=False):
        return self.answers.get(label, default)


@pytest.mark.parametrize("backend", ["codex", "claude_code"])
def test_wizard_wsl_roundtrip_preserves_identity_and_limits(tmp_path, monkeypatch, capsys, backend):
    from acp_gateway.cli.wizard import _agent, _profiles
    from acp_gateway.config import AgentProfile

    monkeypatch.setattr("shutil.which", lambda *a: pytest.fail("no host CLI lookup for WSL"))
    monkeypatch.setattr(
        "acp_gateway.cli.wizard._working_directory",
        lambda *a: pytest.fail("no host cwd validation for WSL"),
    )
    name = "Codex" if backend == "codex" else "Claude Code"
    answers = Answers(
        **{
            "Agent type": backend + "-wsl",
            "WSL distribution (explicit name)": "Ubuntu-22.04",
            "WSL Linux user (non-root)": "alice",
            "Linux working directory in WSL": "/home/alice/project",
            f"{name} Linux executable (absolute POSIX path)": "/opt/cli",
            "WSL agent home (none uses target default)": "/home/alice/agent-config",
            "WSL PATH directories (comma-separated; empty uses explicit executable only)": (
                "/usr/bin,/opt/bin"
            ),
        }
    )
    result = _agent(answers, {})
    assert result[backend]["wsl"] == {
        "distribution": "Ubuntu-22.04",
        "user": "alice",
        "python": "/usr/bin/python3",
        "agent_home": "/home/alice/agent-config",
        "path": ["/usr/bin", "/opt/bin"],
    }
    assert result["default_cwd"] == "/home/alice/project"
    result[backend]["wsl"].update(home="/srv/private-home", python="/opt/python3")
    result[backend].update(model="custom", request_timeout=51, turn_timeout=777)
    old = AgentProfile.model_validate(result).model_dump(mode="json", exclude_none=True)
    raw = {"agents": [old]}
    _profiles(
        Answers(**{"Agent profiles": "edit", "Edit " + old["alias"] + "?": True}),
        raw,
        {},
        "connector",
    )
    # The default idle timeout is left out so older installations can read it.
    assert raw["agents"] == [omit_default_idle_timeout(old)]
    output = capsys.readouterr().out
    assert "target-side" in output and "Ubuntu-22.04" in output and "alice" in output


@pytest.mark.parametrize("backend", ["codex", "claude_code"])
def test_config_check_wsl_environment_badge_without_settings_dump(tmp_path, capsys, backend):
    from acp_gateway.cli.main import _cmd_config_check

    cfg = configured(tmp_path, backend)
    assert _cmd_config_check(cfg) == 0
    out = capsys.readouterr().out
    assert "environment: WSL Ubuntu-22.04/alice" in out
    assert "/opt/agent/bin/cli" not in out


@pytest.mark.parametrize("backend", ["codex", "claude_code"])
def test_wizard_wsl_switches_clear_incompatible_settings(tmp_path, monkeypatch, backend):
    from acp_gateway.cli.wizard import _agent

    name = "Codex" if backend == "codex" else "Claude Code"
    old = {
        "alias": "code",
        "kind": backend,
        "backend": backend,
        "default_cwd": "/home/alice/project",
        backend: {
            "executable": "/opt/cli",
            "model": "custom",
            "request_timeout": 51,
            "turn_timeout": 777,
            "wsl": {
                "distribution": "Ubuntu-22.04",
                "user": "alice",
                "python": "/usr/bin/python3",
                "path": ["/opt/bin"],
            },
        },
    }
    monkeypatch.setattr("shutil.which", lambda *a: None)
    native = _agent(
        Answers(
            **{
                "Agent type": backend,
                "Working directory on the agent computer": str(tmp_path),
            }
        ),
        {},
        old=old,
    )
    assert "wsl" not in native[backend]
    assert native[backend]["executable"] == ("codex" if backend == "codex" else "claude")
    assert native[backend]["model"] == "custom" and native[backend]["turn_timeout"] == 777
    native["default_cwd"] = "C:\\WindowsProject"
    restored = _agent(
        Answers(
            **{
                "Agent type": backend + "-wsl",
                "WSL distribution (explicit name)": "Debian",
                "WSL Linux user (non-root)": "bob",
                "Linux working directory in WSL": "/srv/project",
                f"{name} Linux executable (absolute POSIX path)": "/srv/cli",
            }
        ),
        {},
        old=native,
    )
    assert restored[backend]["wsl"]["distribution"] == "Debian"
    assert restored[backend]["wsl"]["user"] == "bob"
    assert restored[backend]["model"] == "custom" and restored[backend]["request_timeout"] == 51
    other = "claude_code" if backend == "codex" else "codex"
    switched = _agent(
        Answers(
            **{
                "Agent type": other,
                "Working directory on the agent computer": str(tmp_path),
            }
        ),
        {},
        old=old,
    )
    assert backend not in switched
    assert "wsl" not in switched[other] and "model" not in switched[other]
    routed = _agent(Answers(**{"Agent type": backend}), {}, old=old, routed=True)
    assert routed["backend"] == "connector" and backend not in routed
    assert old[backend]["wsl"]["distribution"] == "Ubuntu-22.04"


@pytest.mark.parametrize("backend", ["codex", "claude_code"])
@pytest.mark.parametrize("bad", ["C:\\project", "relative/project", "//host/share", "/bad\\path"])
def test_wizard_rejects_non_posix_wsl_cwd(tmp_path, backend, bad):
    from acp_gateway.cli.wizard import _agent

    with pytest.raises(ValueError):
        _agent(
            Answers(
                **{
                    "Agent type": backend + "-wsl",
                    "WSL distribution (explicit name)": "Ubuntu-22.04",
                    "WSL Linux user (non-root)": "alice",
                    "Linux working directory in WSL": bad,
                }
            ),
            {},
        )


@pytest.mark.parametrize("value", ["/home/alice/../bob", "/c:/windows/style", "/home/\u200balice"])
def test_wsl_cwd_prompt_rejects_config_invalid_paths(value):
    from acp_gateway.cli.wizard import _linux_path

    with pytest.raises(ValueError):
        _linux_path(value)
