"""Onboarding persistence, cancellation, role launch and rollback contracts."""

import json
import os
import socket

import pytest
import yaml
from dotenv import dotenv_values

from acp_gateway.cli import wizard as w
from acp_gateway.cli.main import main
from acp_gateway.config import Settings, load_config
from acp_gateway.storage.db import Store

CHECK_PORTS = w._check_ports


class Answers:
    def __init__(self, **answers):
        self.answers = answers

    def ask(self, label, default="", validate=None, **kwargs):
        value = self.answers.get(label, default)
        return validate(value) if validate else value

    def yes(self, label, default=False):
        return self.answers.get(label, False)

    def choice(self, label, choices, default):
        return w._choice(self.answers.get(label, default), choices)


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch, tmp_path):
    monkeypatch.setattr(w.paths, "data_dir", lambda: tmp_path / "platform-data")
    monkeypatch.setattr(w, "_managed_running", lambda *args: False)
    which = w.shutil.which
    monkeypatch.setattr(
        w.shutil, "which", lambda name: which(name) if name == "powershell.exe" else None
    )
    monkeypatch.setattr(w, "_check_ports", lambda settings: None)


def save_answers(**kwargs):
    return Answers(**{"Save these settings?": True, **kwargs})


def test_local_setup_repeat_keeps_keys_and_unrelated_settings(tmp_path, capsys):
    directory = tmp_path / "settings"
    prompts = save_answers(**{"Agent secret (empty if none)": "private-agent-credential"})
    assert w.wizard(directory, prompts=prompts) == 0
    config, env = directory / "config.yaml", directory / ".env"
    before = dotenv_values(env)
    raw = yaml.safe_load(config.read_text())
    raw["policy"] = {"allow_new_sessions": False}
    config.write_text(yaml.safe_dump(raw))
    assert w.wizard(directory, prompts=save_answers()) == 0
    assert dotenv_values(env) == before
    cfg = load_config(config, env)
    assert not cfg.settings.policy.allow_new_sessions
    assert cfg.settings.agents[0].alias == "goose"
    assert json.loads((directory / w.STATE).read_text())["role"] == "local"
    assert list((directory / "setup-backups").glob("*/manifest.json"))
    if os.name == "posix":
        for path in (config, env, directory / w.STATE):
            assert path.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert all(value not in out for value in before.values())


def test_cancel_does_not_create_configuration(tmp_path):
    directory = tmp_path / "cancelled"
    assert w.wizard(directory, prompts=Answers()) == 0
    assert not directory.exists()


def test_server_enrollment_and_connector_start(tmp_path, monkeypatch):
    server = tmp_path / "server"
    server.mkdir()
    (server / "config.yaml").write_text(yaml.safe_dump({"data_dir": str(tmp_path / "data")}))
    assert (
        w.wizard(
            server,
            prompts=save_answers(
                **{
                    "Role": "server",
                    "Export enrollment keys for new configured computers?": True,
                }
            ),
        )
        == 0
    )
    key = server / "enrollment" / "home.key"
    assert key.is_file()
    before = key.read_bytes()
    assert (
        w.wizard(
            server,
            prompts=save_answers(
                **{
                    "Role": "server",
                    "Export enrollment keys for new configured computers?": True,
                }
            ),
        )
        == 0
    )
    assert key.read_bytes() == before
    connector = tmp_path / "connector"
    assert (
        w.wizard(
            connector,
            prompts=save_answers(
                **{
                    "Role": "connector",
                    "Server connection URL": "wss://server.example/acpgw/connect",
                    "Enrollment key file copied privately from the server": str(key),
                }
            ),
        )
        == 0
    )
    calls = []
    monkeypatch.setattr("acp_gateway.cli.main.main", lambda args: calls.append(args) or 0)
    assert w.start(connector / "config.yaml") == 0
    assert calls[0][2:4] == ["--env-file", str(connector / ".env")]
    assert calls[0][4:] == [
        "connector",
        "--dispatcher-url",
        "wss://server.example/acpgw/connect",
        "--computer-id",
        "home",
        "--token-file",
        str(key),
    ]
    store = Store.open_in(tmp_path / "data")
    try:
        assert [c["computer_id"] for c in store.computers.list()] == ["home"]
    finally:
        store.close()


def test_hermes_preserves_other_servers_and_private_token(tmp_path):
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    (hermes / "config.yaml").write_text("mcp_servers:\n  other:\n    command: other\n")
    (hermes / ".env").write_text("OTHER_TOKEN=keep\n")
    directory = tmp_path / "gateway"
    assert (
        w.wizard(
            directory,
            prompts=save_answers(
                **{
                    "Connect this gateway to a local Hermes installation?": True,
                    "Hermes configuration directory": str(hermes),
                }
            ),
        )
        == 0
    )
    config = yaml.safe_load((hermes / "config.yaml").read_text())
    assert config["mcp_servers"]["other"] == {"command": "other"}
    assert config["mcp_servers"]["acpgw"]["headers"]["Authorization"] == "Bearer ${ACPGW_MCP_TOKEN}"
    gateway_env = dotenv_values(directory / ".env")
    hermes_env = dotenv_values(hermes / ".env")
    assert hermes_env == {"OTHER_TOKEN": "keep", "ACPGW_MCP_TOKEN": gateway_env["ACPGW_MCP_TOKEN"]}


def test_save_refuses_concurrent_edit_and_restores_partial_write(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"old")
    originals = {first: b"old", second: None}
    first.write_bytes(b"concurrent edit")
    with pytest.raises(ValueError, match="changed during setup"):
        w.save_files({first: b"new", second: b"new"}, originals, tmp_path)
    assert first.read_bytes() == b"concurrent edit"
    first.write_bytes(b"old")
    atomic = w._atomic

    def fail(path, content):
        if path == second:
            raise OSError("disk error")
        atomic(path, content)

    monkeypatch.setattr(w, "_atomic", fail)
    with pytest.raises(OSError, match="disk error"):
        w.save_files({first: b"new", second: b"new"}, originals, tmp_path)
    assert first.read_bytes() == b"old"
    assert not second.exists()


def test_ports_reject_existing_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        settings = Settings(gateway={"port": listener.getsockname()[1]})
        with pytest.raises(ValueError, match="Cannot bind"):
            CHECK_PORTS(settings)


def test_secret_prompt_hides_existing_value_and_numeric_choices(monkeypatch, capsys):
    labels = []
    monkeypatch.setattr(w.getpass, "getpass", lambda label: labels.append(label) or "")
    assert w.Prompts().ask("Secret", "hidden-value", secret=True) == "hidden-value"
    assert "hidden-value" not in labels[0]
    assert w._choice("2", ("local", "server")) == "server"
    with pytest.raises(ValueError):
        w._choice("3", ("local", "server"))


def test_interactive_commands_require_terminal(capsys):
    assert main(["setup", "--interactive"]) == 1
    assert main(["config", "edit"]) == 1
    assert main(["menu"]) == 1
    assert "requires a terminal" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="systemd unit rollback is POSIX-only")
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_service_start_failure_restores_configuration_and_unit(
    tmp_path, monkeypatch, cleanup_fails
):
    from acp_gateway import service

    directory = tmp_path / "gateway"
    w.wizard(directory, prompts=save_answers())
    paths = [directory / "config.yaml", directory / ".env", directory / w.STATE]
    before = {path: path.read_bytes() for path in paths}
    unit = tmp_path / "gateway.service"
    original_unit = service.MARKER + service._quote(str(directory / "config.yaml"))
    unit.write_text(original_unit)
    monkeypatch.setattr(w.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "unit_path", lambda role: unit)
    monkeypatch.setattr(service, "_systemctl", lambda *args, **kwargs: "UnitFileState=disabled")
    operations = []

    def install(*args, **kwargs):
        unit.write_text("replacement")

    def control(action, **kwargs):
        operations.append(action)
        if action == "enable":
            raise ValueError("failed to start")
        if action == "stop" and cleanup_fails:
            raise ValueError("manager unavailable")

    monkeypatch.setattr(service, "install", install)
    monkeypatch.setattr(service, "control", control)
    with pytest.raises(ValueError, match="failed to start"):
        w.wizard(
            directory,
            prompts=save_answers(
                **{
                    "Local API/MCP port (loopback only)": "18765",
                    "Install and start the systemd user service?": True,
                }
            ),
        )
    assert {path: path.read_bytes() for path in paths} == before
    assert unit.read_text() == original_unit
    assert operations == ["enable", "stop"]


def test_profile_edit_preserves_advanced_options_and_remove_keeps_other_agent(tmp_path):
    raw = {
        "agents": [
            {
                "alias": "goose",
                "kind": "goose",
                "url": "ws://192.0.2.1/acp",
                "default_cwd": str(tmp_path),
                "allow_insecure_transport": True,
                "session_mode": "manual",
            },
            {
                "alias": "other",
                "kind": "generic",
                "url": "ws://127.0.0.1:3285/acp",
                "default_cwd": "/other",
            },
        ]
    }
    w._profiles(Answers(**{"Agent profiles": "edit", "Edit goose?": True}), raw, {}, "local")
    assert raw["agents"][0]["allow_insecure_transport"]
    assert raw["agents"][0]["session_mode"] == "manual"
    w._profiles(
        Answers(
            **{
                "Agent profiles": "remove",
                "Remove goose? (saved secrets are kept)": True,
            }
        ),
        raw,
        {},
        "local",
    )
    assert [a["alias"] for a in raw["agents"]] == ["other"]


def test_start_refuses_duplicate_managed_service(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_managed_running", lambda *args: True)
    with pytest.raises(ValueError, match="already running"):
        w.start(tmp_path / "config.yaml")


def test_partial_settings_preserve_agents_and_credentials(tmp_path):
    w.wizard(tmp_path, prompts=save_answers())
    original = yaml.safe_load((tmp_path / "config.yaml").read_text())["agents"]
    before = dotenv_values(tmp_path / ".env")
    w.wizard(
        tmp_path,
        section="telegram",
        prompts=save_answers(
            **{
                "Enable Telegram?": True,
                "Telegram bot token from BotFather": "telegram-key-private",
                "Allowed Telegram user IDs, separated by commas": "123,456",
            }
        ),
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.settings.telegram.allowed_user_ids == [123, 456]
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["agents"] == original
    assert all(dotenv_values(tmp_path / ".env")[key] == value for key, value in before.items())


@pytest.mark.parametrize("during_setup", [False, True])
def test_connect_from_local_setup_or_later_without_reentering_profiles(tmp_path, during_setup):
    key = tmp_path / "enrollment.key"
    key.write_text("acpc_" + "a" * 43)
    key.chmod(0o600)
    answers = save_answers(
        **{
            "Connect this computer to an external dispatcher now?": True,
            "Enrollment key file copied privately from the server": str(key),
            "Server connection URL": "wss://dispatcher.example/acpgw/connect",
        }
    )
    if during_setup:
        w.wizard(tmp_path, prompts=answers)
    else:
        w.wizard(tmp_path, prompts=save_answers())
        before = dotenv_values(tmp_path / ".env")
        w.wizard(tmp_path, section="connection", prompts=answers)
        assert dotenv_values(tmp_path / ".env") == before
    state = json.loads((tmp_path / w.STATE).read_text())
    assert state["role"] == "connector"
    assert state["connection"]["dispatcher_url"] == "wss://dispatcher.example/acpgw/connect"
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.settings.agents[0].alias == "goose"
    assert not cfg.settings.connector.enabled
    assert not cfg.settings.telegram.enabled
