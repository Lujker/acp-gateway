"""Claude setup and diagnostics are explicit, native and model-free."""

import copy
from types import SimpleNamespace

import pytest

from acp_gateway.cli.main import _cmd_config_check
from acp_gateway.cli.wizard import _agent, _profiles
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings


class Answers:
    def __init__(self, **answers):
        self.answers = answers
        self.asked = []

    def ask(self, label, default="", validate=None, **kwargs):
        self.asked.append(label)
        value = self.answers.get(label, default)
        return validate(value) if validate else value

    def choice(self, label, choices, default):
        assert self.answers.get(label, default) in choices
        return self.answers.get(label, default)

    def yes(self, label, default=False):
        return self.answers.get(label, default)


def config(tmp_path):
    profile = AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd=str(tmp_path)
    )
    return AppConfig(Settings(agents=[profile]), SecretStore(), None, None)


@pytest.mark.parametrize("role", ["local", "connector", "server"])
def test_wizard_creates_claude_for_each_setup_role_without_install_login_or_secrets(
    tmp_path, monkeypatch, capsys, role
):
    import acp_gateway.cli.wizard as wizard

    monkeypatch.setattr(wizard, "shutil", SimpleNamespace(which=lambda name: None))
    monkeypatch.setattr(
        wizard, "subprocess", SimpleNamespace(Popen=lambda *a, **kw: pytest.fail("must not spawn"))
    )
    raw, values = {}, {}
    _profiles(
        Answers(
            **{
                "Agent type": "claude_code",
                "Working directory on the agent computer": str(tmp_path),
            }
        ),
        raw,
        values,
        role,
    )
    profile = AgentProfile.model_validate(raw["agents"][0])
    assert profile.alias == "claude" and profile.title == "Claude Code"
    assert profile.backend == ("connector" if role == "server" else "claude_code")
    assert not profile.url and not profile.secret_env and not profile.session_mode
    assert values == {}
    if role == "server":
        assert profile.claude_code is None
        assert raw["connector"]["enabled"]
    else:
        assert profile.claude_code.executable == "claude"
        output = capsys.readouterr().out
        assert "PATH" in output and "login separately" in output
        assert "not an OS sandbox" in output


def test_wizard_edit_preserves_claude_limits_timeouts_and_clears_model(tmp_path):
    old = config(tmp_path).settings.agents[0].model_dump(exclude_none=True)
    old["claude_code"].update(
        model="custom/model", request_timeout=51, turn_timeout=777, max_turns=34, max_sessions=12
    )
    original = copy.deepcopy(old)
    edited = _agent(Answers(**{"Display name": "Edited"}), {}, old=old)
    assert edited["claude_code"] == old["claude_code"] and old == original
    cleared = _agent(
        Answers(**{"Claude Code model (none uses CLI default)": "none"}), {}, old=edited
    )
    assert "model" not in cleared["claude_code"]
    assert cleared["claude_code"]["max_turns"] == 34


@pytest.mark.parametrize("kind", ["generic", "codex"])
@pytest.mark.parametrize("routed", [False, True])
def test_switching_claude_removes_incompatible_settings(tmp_path, kind, routed):
    old = config(tmp_path).settings.agents[0].model_dump(exclude_none=True)
    result = _agent(Answers(**{"Agent type": kind}), {}, old=old, routed=routed)
    profile = AgentProfile.model_validate(result)
    assert profile.claude_code is None
    assert profile.kind == kind


def test_switching_acp_to_claude_strips_transport_and_mode(tmp_path):
    old = {
        "alias": "old",
        "kind": "goose",
        "url": "wss://example.org",
        "secret_env": "OLD",
        "tls_fingerprint": "AA" * 32,
        "allow_insecure_transport": True,
        "session_mode": "auto",
        "default_cwd": str(tmp_path),
    }
    profile = AgentProfile.model_validate(
        _agent(Answers(**{"Agent type": "claude_code"}), {}, old=old)
    )
    assert profile.backend == "claude_code" and profile.session_mode is None
    assert not profile.url and not profile.secret_env and not profile.tls_fingerprint
    assert not profile.allow_insecure_transport


def test_config_check_claude_guidance_does_not_expose_settings_or_claim_read_only(tmp_path, capsys):
    cfg = config(tmp_path)
    cfg.settings.agents[0].claude_code.model = "secret-model"
    assert _cmd_config_check(cfg) == 0
    output = capsys.readouterr().out
    assert "managed: local Claude Code" in output
    assert "not an OS sandbox" in output
    assert "secret-model" not in output and "url:" not in output


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_doctor_native_claude_uses_owned_runner_and_only_initializes(
    tmp_path, monkeypatch, live, fails
):
    import asyncio
    import shutil

    import acp_gateway.cli.doctor as doctor
    from acp_gateway.cli import setup_asyncio

    cfg = config(tmp_path)
    cfg.settings.agents[0].claude_code.executable = shutil.which("python") or shutil.which(
        "python3"
    )
    path = tmp_path / "config.yaml"
    path.write_text(cfg.settings.model_dump_json(exclude_none=True))
    (tmp_path / ".env").write_text("")
    calls, operations, loops = [], [], []

    class Client:
        def __init__(self, profile, secret, **kwargs):
            assert profile.backend == "claude_code" and secret is None

        async def connect(self):
            calls.append("initialize")
            loops.append(asyncio.get_running_loop())
            if fails:
                raise ValueError("private native diagnostics")

        async def close(self):
            calls.append("close")

    def run(coro, *, operation):
        operations.append(operation)
        return setup_asyncio.run(coro, operation=operation)

    monkeypatch.setattr(doctor, "AgentClient", Client)
    monkeypatch.setattr(doctor, "run_setup", run)
    checks = doctor.collect(path, live=live)
    row = next(c for c in checks if c["name"] == "agent:claude")
    assert "Claude Code" in row["detail"]
    assert (
        "acpgw agents configure" in row["fix"]
        and "PATH" in row["fix"]
        and "login separately" in row["fix"]
    )
    assert "private native diagnostics" not in str(checks)
    if live:
        assert operations == ["doctor-claude-code"]
        assert calls == ["initialize", "close"]
        assert len(loops) == 1 and loops[0].is_closed()
        assert row["status"] == ("error" if fails else "ok")
        if not fails:
            assert "no session or model call" in row["detail"]
    else:
        assert calls == operations == []


def test_offline_doctor_claude_checks_executable_and_cwd(tmp_path):
    from acp_gateway.cli.doctor import collect

    cfg = config(tmp_path)
    cfg.settings.agents[0].claude_code.executable = "missing-private-executable-25791"
    cfg.settings.agents[0].default_cwd = str(tmp_path / "missing")
    path = tmp_path / "config.yaml"
    path.write_text(cfg.settings.model_dump_json(exclude_none=True))
    (tmp_path / ".env").write_text("")
    checks = collect(path)
    assert next(c for c in checks if c["name"] == "cwd:claude")["status"] == "error"
    row = next(c for c in checks if c["name"] == "agent:claude")
    assert row["status"] == "error" and "executable not found" in row["detail"]
    assert "missing-private-executable" not in str(checks)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_claude_preflight_is_initialize_only_with_native_guidance(
    tmp_path, monkeypatch, capsys, fails
):
    from acp_gateway.cli.wizard import _check_agents

    calls = []

    class Client:
        def __init__(self, profile, secret, **kwargs):
            assert profile.backend == "claude_code" and secret is None

        async def connect(self):
            calls.append("initialize")
            if fails:
                raise ValueError("private-error")

        async def close(self):
            calls.append("close")

    monkeypatch.setattr("acp_gateway.agents.client.AgentClient", Client)
    await _check_agents(config(tmp_path))
    assert calls == ["initialize", "close"]
    output = capsys.readouterr().out
    assert "Claude Code initialize" in output
    assert "TLS" not in output and "secret" not in output and "private-error" not in output
    if fails:
        assert "executable" in output and "directory" in output and "login separately" in output
    else:
        assert "no session or model call" in output


def test_real_prompts_retry_claude_executable_and_model(tmp_path, monkeypatch, capsys):
    import acp_gateway.cli.wizard as wizard

    answers = iter(
        [
            "claude_code",
            "",
            "",
            str(tmp_path),
            "relative/claude",
            "/opt/manual claude/claude",
            "bad model",
            "custom/model",
            "safe",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    monkeypatch.setattr(wizard, "shutil", SimpleNamespace(which=lambda name: None))
    result = _agent(wizard.Prompts(), {})
    assert result["claude_code"]["executable"] == "/opt/manual claude/claude"
    assert result["claude_code"]["model"] == "custom/model"
    assert capsys.readouterr().out.count("Invalid value") == 2


@pytest.mark.parametrize("kind", ["codex", "hermes"])
def test_switching_another_managed_backend_to_claude_discards_old_settings(tmp_path, kind):
    old = {"alias": "old", "kind": kind, "backend": kind, kind: {}, "default_cwd": str(tmp_path)}
    result = _agent(Answers(**{"Agent type": "claude_code"}), {}, old=old)
    assert kind not in result
    assert result["backend"] == "claude_code"


def test_routed_claude_does_not_look_up_local_executable(monkeypatch):
    import acp_gateway.cli.wizard as wizard

    monkeypatch.setattr(
        wizard, "shutil", SimpleNamespace(which=lambda *args: pytest.fail("remote-only profile"))
    )
    result = _agent(
        Answers(
            **{
                "Agent type": "claude_code",
                "Working directory on the agent computer": "C:\\remote\\project",
            }
        ),
        {},
        routed=True,
    )
    assert result["backend"] == "connector" and "claude_code" not in result
    assert result["default_cwd"] == "C:\\remote\\project"
