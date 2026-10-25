"""Codex setup, profile editing and readiness never make model calls."""

import copy
import json
from types import SimpleNamespace

import pytest

from acp_gateway.cli.doctor import collect
from acp_gateway.cli.main import _cmd_config_check
from acp_gateway.cli.wizard import _agent, _profiles
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings


def config(tmp_path):
    profile = AgentProfile(alias="code", kind="codex", backend="codex", default_cwd=str(tmp_path))
    return AppConfig(Settings(agents=[profile]), SecretStore(), None, None)


def test_config_check_identifies_managed_codex_without_fake_url(tmp_path, capsys):
    assert _cmd_config_check(config(tmp_path)) == 0
    output = capsys.readouterr().out
    assert "managed: local Codex" in output
    assert "read-only" in output
    assert "url:" not in output


def test_wizard_preserves_manual_codex_connector_profile(tmp_path):
    raw = {"agents": [config(tmp_path).settings.agents[0].model_dump(exclude_none=True)]}
    original = dict(raw["agents"][0])
    prompts = SimpleNamespace(choice=lambda *args: "keep")
    _profiles(prompts, raw, {}, "connector")
    assert raw["agents"] == [original]


class Answers:
    def __init__(self, **answers):
        self.answers = answers
        self.asked = []

    def ask(self, label, default="", validate=None, **kwargs):
        self.asked.append((label, kwargs))
        value = self.answers.get(label, default)
        return validate(value) if validate else value

    def choice(self, label, choices, default):
        from acp_gateway.cli.wizard import _choice

        return _choice(self.answers.get(label, default), choices)

    def yes(self, label, default=False):
        return self.answers.get(label, default)


def test_wizard_creates_local_codex_without_transport_or_secret(tmp_path):
    values = {}
    result = _agent(
        Answers(
            **{"Agent type": "codex", "Working directory on the agent computer": str(tmp_path)}
        ),
        values,
    )
    profile = AgentProfile.model_validate(result)
    assert profile.alias == "codex"
    assert profile.backend == profile.kind == "codex"
    assert profile.codex.executable == "codex"
    assert profile.codex.model is None
    assert profile.codex.sandbox == "read-only"
    assert not profile.url and profile.secret_env is None
    assert profile.session_mode is None
    assert values == {}


def test_offline_doctor_identifies_codex_without_assuming_websocket(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        '{"agents": [{"alias": "code", "kind": "codex", "backend": "codex", "default_cwd": '
        + json.dumps(str(tmp_path))
        + "}]}"
    )
    (tmp_path / ".env").write_text("")
    checks = collect(path)
    row = next(c for c in checks if c["name"] == "agent:code")
    assert "Codex" in row["detail"]
    assert "acpgw agents configure" in row["fix"]
    assert "manual YAML" not in row["fix"]
    assert "PATH" in row["fix"]


@pytest.mark.parametrize("routed", [False, True])
def test_wizard_switches_codex_to_acp_stripping_managed_settings(tmp_path, routed):
    old = config(tmp_path).settings.agents[0].model_dump(exclude_none=True)
    old["codex"].update(model="custom/model", request_timeout=51, turn_timeout=777)
    original = copy.deepcopy(old)
    result = _agent(
        Answers(**{"Agent type": "generic", "ACP URL": "ws://127.0.0.1:3284/acp"}),
        {},
        old=old,
        routed=routed,
    )
    profile = AgentProfile.model_validate(result)
    assert profile.backend == ("connector" if routed else "remote")
    assert profile.kind == "generic"
    assert "codex" not in result
    assert old == original


def test_wizard_missing_codex_reports_manual_repair_without_spawning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("acp_gateway.cli.wizard.shutil.which", lambda executable: None)
    monkeypatch.setattr(
        "acp_gateway.cli.wizard.subprocess.Popen",
        lambda *a, **kw: pytest.fail("setup must not spawn Codex or an installer"),
    )
    prompts = Answers(
        **{
            "Agent type": "codex",
            "Working directory on the agent computer": str(tmp_path),
            "Codex executable name or absolute path": "missing-codex",
        }
    )
    profile = AgentProfile.model_validate(_agent(prompts, {}))
    assert profile.codex.executable == "missing-codex"
    output = capsys.readouterr().out
    assert "not found" in output and "PATH" in output and "absolute path" in output
    assert "login" in output
    assert not any(options.get("secret") for _, options in prompts.asked)


def test_wizard_retries_duplicate_codex_edit_before_mutating_profiles(tmp_path, capsys):
    first = config(tmp_path).settings.agents[0].model_dump(exclude_none=True)
    second = {**first, "alias": "other"}
    raw = {"agents": [first, second]}
    original = copy.deepcopy(raw)

    class Edit(Answers):
        aliases = iter(("other", "edited"))

        def ask(self, label, default="", validate=None, **kwargs):
            if label == "Agent alias":
                assert raw == original
                return validate(next(self.aliases))
            return super().ask(label, default, validate, **kwargs)

    _profiles(Edit(**{"Agent profiles": "edit", "Edit code?": True}), raw, {}, "connector")
    assert [a["alias"] for a in raw["agents"]] == ["edited", "other"]
    assert original["agents"] == [first, second]
    assert "already exists" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_wizard_codex_preflight_only_initializes_and_reports_native_advice(
    tmp_path, monkeypatch, capsys, fails
):
    from acp_gateway.cli.wizard import _check_agents

    calls = []

    class Client:
        def __init__(self, profile, secret, **kwargs):
            assert profile.backend == "codex" and secret is None

        async def connect(self):
            calls.append("initialize")
            if fails:
                raise ValueError("not ready")

        async def close(self):
            calls.append("close")

    monkeypatch.setattr("acp_gateway.agents.client.AgentClient", Client)
    await _check_agents(config(tmp_path))
    assert calls == ["initialize", "close"]
    output = capsys.readouterr().out
    assert "Codex" in output
    assert "TLS" not in output and "secret" not in output and "ACP" not in output
    if fails:
        assert "executable" in output and "directory" in output and "login" in output
    else:
        assert "initialize passed" in output and "no thread or model" in output


@pytest.mark.parametrize("routed", [False, True])
def test_wizard_switches_acp_to_codex_and_drops_stale_transport(tmp_path, routed):
    old = {
        "alias": "existing",
        "kind": "goose",
        "backend": "remote",
        "url": "wss://agent.example/acp",
        "secret_env": "OLD_SECRET",
        "tls_fingerprint": "AA" * 32,
        "allow_insecure_transport": True,
        "default_cwd": str(tmp_path),
        "session_mode": "custom",
    }
    result = _agent(Answers(**{"Agent type": "codex"}), {}, old=old, routed=routed)
    profile = AgentProfile.model_validate(result)
    assert profile.alias == "existing"
    assert not profile.url and profile.secret_env is None and profile.tls_fingerprint is None
    assert not profile.allow_insecure_transport and profile.session_mode is None
    assert profile.hermes is None
    assert profile.backend == ("connector" if routed else "codex")
    assert (profile.codex is None) == routed


def test_wizard_edits_codex_preserving_unedited_custom_settings(tmp_path):
    old = config(tmp_path).settings.agents[0].model_dump(exclude_none=True)
    old["codex"].update(
        executable="/opt/manual-codex",
        model="custom/model",
        sandbox="workspace-write",
        request_timeout=51,
        turn_timeout=777,
    )
    result = _agent(Answers(**{"Display name": "Edited title"}), {}, old=old)
    assert result["codex"] == old["codex"]
    assert result["title"] == "Edited title"
    cleared = _agent(Answers(**{"Codex model (none uses CLI default)": "none"}), {}, old=result)
    assert "model" not in cleared["codex"]
    assert cleared["codex"]["request_timeout"] == 51


def test_wizard_routed_codex_uses_remote_cwd_without_executable_lookup(monkeypatch):
    monkeypatch.setattr(
        "acp_gateway.cli.wizard.shutil.which",
        lambda *args: pytest.fail("routed Codex must not inspect local executable"),
    )
    result = _agent(
        Answers(
            **{
                "Agent type": "codex",
                "Working directory on the agent computer": "C:\\remote\\project",
            }
        ),
        {},
        routed=True,
    )
    assert result["alias"] == "codex" and result["computer_id"] == "home"
    assert result["backend"] == "connector" and "codex" not in result


def test_real_prompts_retry_codex_executable_model_and_sandbox(tmp_path, monkeypatch, capsys):
    from acp_gateway.cli.wizard import Prompts

    answers = iter(
        (
            "codex",
            "",
            "",
            str(tmp_path),
            "--bad",
            "relative/codex",
            "/opt/manual codex/codex",
            "bad model",
            "custom/model",
            "safe",
            "danger-full-access",
            "workspace-write",
        )
    )
    monkeypatch.setattr("builtins.input", lambda label: next(answers))
    result = _agent(Prompts(), {})
    assert result["codex"]["executable"] == "/opt/manual codex/codex"
    assert result["codex"]["model"] == "custom/model"
    assert result["codex"]["sandbox"] == "workspace-write"
    assert capsys.readouterr().out.count("Invalid value") == 4


@pytest.mark.parametrize("role", ["local", "connector", "server"])
def test_normal_agent_section_saves_codex_for_each_setup_role(tmp_path, monkeypatch, role):
    import yaml

    from acp_gateway.cli import wizard as w
    from acp_gateway.config import load_config

    monkeypatch.setattr(w, "_managed_running", lambda *a: False)
    monkeypatch.setattr(w, "_check_ports", lambda *a: None)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump({"data_dir": str(tmp_path / "data")}))
    (tmp_path / w.STATE).write_text(json.dumps({"version": 1, "role": role}))
    assert (
        w.wizard(
            tmp_path,
            section="agents",
            prompts=Answers(
                **{
                    "Agent type": "codex",
                    "Working directory on the agent computer": str(tmp_path),
                    "Save these settings?": True,
                }
            ),
        )
        == 0
    )
    cfg = load_config(config_file, tmp_path / ".env")
    profile = cfg.settings.agents[0]
    assert profile.alias == "codex" and profile.kind == "codex"
    assert profile.backend == ("connector" if role == "server" else "codex")


def test_wizard_switch_from_codex_offers_usable_acp_url_default(tmp_path):
    old = config(tmp_path).settings.agents[0].model_dump(exclude_none=True)
    result = _agent(Answers(**{"Agent type": "generic"}), {}, old=old)
    assert result["url"] == "ws://127.0.0.1:3284/acp"
    assert "codex" not in result


def test_invalid_connector_codex_route_edit_does_not_mutate_raw(tmp_path):
    raw = {
        "agents": [
            {
                "alias": "code",
                "kind": "codex",
                "backend": "connector",
                "computer_id": "other",
                "default_cwd": str(tmp_path),
            }
        ]
    }
    original = copy.deepcopy(raw)
    with pytest.raises(ValueError, match="computer connector needs local"):
        _profiles(
            Answers(
                **{
                    "Agent profiles": "edit",
                    "Edit code?": True,
                    "Agent alias": "changed",
                }
            ),
            raw,
            {},
            "connector",
        )
    assert raw == original


def test_fresh_local_setup_selects_codex_without_installing_or_running_it(tmp_path, monkeypatch):
    from acp_gateway.cli import wizard as w
    from acp_gateway.config import load_config

    monkeypatch.setattr(w, "_managed_running", lambda *a: False)
    monkeypatch.setattr(w, "_check_ports", lambda *a: None)
    monkeypatch.setattr(w.paths, "data_dir", lambda: tmp_path / "data")
    # Keep native Windows ACL verification real: patch wizard-local proxies,
    # not the stdlib modules also imported by the ACL verifier.
    monkeypatch.setattr(
        w, "shutil", SimpleNamespace(**{**vars(w.shutil), "which": lambda *a: None})
    )
    monkeypatch.setattr(w, "check_connections", lambda *a: pytest.fail("check was declined"))
    monkeypatch.setattr(
        w,
        "subprocess",
        SimpleNamespace(
            **{**vars(w.subprocess), "Popen": lambda *a, **k: pytest.fail("unexpected spawn")}
        ),
    )
    prompts = Answers(
        **{
            "Agent type": "codex",
            "Working directory on the agent computer": str(tmp_path),
            "Check connections now (no model prompts or messages)?": False,
            "Save these settings?": True,
        }
    )
    assert w.wizard(tmp_path / "settings", prompts=prompts) == 0
    cfg = load_config(tmp_path / "settings/config.yaml", tmp_path / "settings/.env")
    assert [(a.alias, a.backend) for a in cfg.settings.agents] == [("codex", "codex")]
    assert not any(options.get("secret") for _, options in prompts.asked)


def test_wizard_adds_multiple_selected_codex_profiles_and_removes_only_selected(tmp_path):
    class Multiple(Answers):
        aliases = iter(("first", "second"))
        another = iter((True, False))

        def ask(self, label, default="", validate=None, **kwargs):
            if label == "Agent alias":
                return validate(next(self.aliases))
            return super().ask(label, default, validate, **kwargs)

        def yes(self, label, default=False):
            return next(self.another) if label == "Add another agent?" else default

    raw = {}
    _profiles(
        Multiple(
            **{
                "Agent type": "codex",
                "Working directory on the agent computer": str(tmp_path),
            }
        ),
        raw,
        {},
        "connector",
    )
    assert [(a["alias"], a["kind"], a["backend"]) for a in raw["agents"]] == [
        ("first", "codex", "codex"),
        ("second", "codex", "codex"),
    ]
    preserved = copy.deepcopy(raw["agents"][0])
    _profiles(
        Answers(
            **{
                "Agent profiles": "remove",
                "Remove agent": "second",
                "Remove second? (saved secrets are kept)": True,
            }
        ),
        raw,
        {},
        "connector",
    )
    assert raw["agents"] == [preserved]
