"""Independent setup, section isolation and partial connector readiness."""

import shutil

import pytest
from dotenv import dotenv_values
from test_wizard import Answers, save_answers

from acp_gateway.cli.main import main
from acp_gateway.cli.setup_menu import setup_menu
from acp_gateway.cli.wizard import check_connections, start, wizard
from acp_gateway.config import load_config
from acp_gateway.connectors.launch import ConnectorLaunch
from acp_gateway.setup_state import STATE, parse


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr("acp_gateway.cli.wizard._managed_running", lambda *args: False)
    monkeypatch.setattr("acp_gateway.cli.wizard._check_ports", lambda *args: None)
    which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: which(name) if name == "powershell.exe" else None
    )


class Sections(Answers):
    def __init__(self, actions, **answers):
        super().__init__(**{"Save these settings?": True, **answers})
        self.actions = iter(actions)

    def choice(self, label, choices, default):
        if label == "Setup section":
            return next(self.actions)
        return super().choice(label, choices, default)


def base(path, role="server"):
    assert wizard(path, section="base", role=role, prompts=save_answers()) == 0
    return load_config(path / "config.yaml", path / ".env")


@pytest.mark.parametrize("role", ["server", "local", "connector"])
def test_base_never_requests_optional_sections(tmp_path, monkeypatch, role):
    for name in (
        "_profiles",
        "_telegram",
        "_hermes",
        "_connection",
        "_enroll",
        "check_connections",
    ):
        monkeypatch.setattr(
            "acp_gateway.cli.wizard." + name, lambda *a: pytest.fail("optional step")
        )
    cfg = base(tmp_path, role)
    assert not cfg.settings.agents
    assert not cfg.settings.telegram.enabled
    assert cfg.settings.connector.enabled is (role == "server")
    state = parse((tmp_path / STATE).read_bytes())
    assert state["role"] == role
    assert "connection" not in state
    assert set(dotenv_values(tmp_path / ".env")) == {"ACPGW_API_TOKEN", "ACPGW_MCP_TOKEN"}


def test_exit_from_fresh_setup_leaves_no_files(tmp_path):
    directory = tmp_path / "not-created"
    assert setup_menu(directory, prompts=Sections(["exit"])) == 0
    assert not directory.exists()


def test_menu_recovers_from_premature_step_and_cancel(tmp_path, capsys):
    directory = tmp_path / "settings"
    prompts = Sections(["telegram", "base", "exit"], **{"Save these settings?": False})
    assert setup_menu(directory, prompts=prompts) == 0
    assert "Complete base setup first" in capsys.readouterr().err
    assert not directory.exists()


def test_setup_menu_first_base_then_telegram_keeps_keys_and_no_agents(tmp_path):
    prompts = Sections(
        ["base", "telegram", "exit"],
        **{
            "Enable Telegram?": True,
            "Telegram bot token from BotFather": "private-test-bot-token",
            "Allowed Telegram user IDs, separated by commas": "12345",
        },
    )
    assert setup_menu(tmp_path, role="server", prompts=prompts) == 0
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.settings.telegram.enabled
    assert cfg.settings.telegram.allowed_user_ids == [12345]
    assert not cfg.settings.agents
    before = dotenv_values(tmp_path / ".env")
    wizard(tmp_path, section="gateway", prompts=save_answers())
    assert dotenv_values(tmp_path / ".env") == before
    assert not (tmp_path / "enrollment").exists()


@pytest.mark.parametrize(
    "section,field,label,value",
    [
        ("rate-limits", "work_requests", "Rate limits: work requests", "17"),
        ("retention", "jobs_days", "Retention: jobs days", "5"),
        ("logging", "level", "Logging: level", "DEBUG"),
        ("policy", "max_prompt_length", "Policy: max prompt length", "1234"),
    ],
)
def test_operational_sections_are_independent(tmp_path, section, field, label, value):
    base(tmp_path)
    before = dotenv_values(tmp_path / ".env")
    wizard(tmp_path, section=section, prompts=save_answers(**{label: value}))
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    actual = getattr(getattr(cfg.settings, section.replace("-", "_")), field)
    assert str(actual) == value
    assert not cfg.settings.agents
    assert not cfg.settings.telegram.enabled
    assert dotenv_values(tmp_path / ".env") == before


def test_invalid_section_answer_does_not_write_anything(tmp_path):
    base(tmp_path)
    files = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        wizard(
            tmp_path,
            section="rate-limits",
            prompts=save_answers(**{"Rate limits: work requests": "0"}),
        )
    assert all(p.read_bytes() == content for p, content in files.items())


def test_connector_base_can_add_connection_and_agents_in_either_order(tmp_path):
    cfg = base(tmp_path, "connector")
    with pytest.raises(ValueError, match="No saved connector connection"):
        ConnectorLaunch.from_saved(cfg)
    key = tmp_path / "home.key"
    key.write_text("acpc_" + "a" * 43)
    key.chmod(0o600)
    wizard(
        tmp_path,
        section="connection",
        prompts=save_answers(
            **{
                "Enrollment key file copied privately from the server": str(key),
                "Server connection URL": "wss://server.example/acpgw/connect",
            }
        ),
    )
    assert not load_config(cfg.config_path, cfg.env_file).settings.agents
    wizard(tmp_path, section="agents", prompts=save_answers())
    cfg = load_config(cfg.config_path, cfg.env_file)
    assert ConnectorLaunch.from_saved(cfg).computer_id == "home"
    before = key.read_bytes()
    wizard(tmp_path, section="logging", prompts=save_answers())
    assert key.read_bytes() == before
    assert (
        ConnectorLaunch.from_saved(load_config(cfg.config_path, cfg.env_file)).computer_id == "home"
    )


def test_server_connection_action_does_not_change_role(tmp_path):
    base(tmp_path)
    before = (tmp_path / STATE).read_bytes()
    with pytest.raises(ValueError, match="Outgoing connection belongs"):
        wizard(tmp_path, section="connection", prompts=save_answers())
    assert (tmp_path / STATE).read_bytes() == before


def test_remove_last_server_agent_keeps_server_operational(tmp_path):
    base(tmp_path)
    wizard(tmp_path, section="agents", prompts=save_answers())
    wizard(
        tmp_path,
        section="agents",
        prompts=save_answers(
            **{
                "Agent profiles": "remove",
                "Remove home/goose? (saved secrets are kept)": True,
            }
        ),
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.settings.agents == []
    assert cfg.settings.connector.enabled


def test_partial_connector_diagnostics_and_start_are_actionable(tmp_path, capsys):
    cfg = base(tmp_path, "connector")
    check_connections(cfg, {"role": "connector"})
    assert "not configured" in capsys.readouterr().out
    with pytest.raises(ValueError, match="No saved connector connection"):
        start(cfg.config_path, cfg.env_file)


def test_setup_cli_routes_to_menu_and_direct_section(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    calls = []
    monkeypatch.setattr(
        "acp_gateway.cli.setup_menu.setup_menu", lambda *a, **kw: calls.append(kw) or 0
    )
    assert main(["setup", "--config-dir", str(tmp_path), "--role", "server"]) == 0
    assert calls == [{"config_file": None, "env_file": None, "role": "server"}]
    monkeypatch.setattr("acp_gateway.cli.wizard.wizard", lambda *a, **kw: calls.append(kw) or 0)
    assert main(["setup", "--section", "telegram"]) == 0
    assert calls[-1]["section"] == "telegram"


def test_setup_service_and_computers_retain_explicit_paths(tmp_path):
    cfg = base(tmp_path)
    env = tmp_path / "custom.env"
    env.write_bytes(cfg.env_file.read_bytes())
    calls = []
    setup_menu(
        tmp_path,
        env_file=env,
        prompts=Sections(["service", "computers", "exit"]),
        execute=lambda args: calls.append(args) or 0,
    )
    prefix = ["--config", str(cfg.config_path), "--env-file", str(env)]
    assert calls == [
        [*prefix, "service", "--role", "gateway", "status"],
        [*prefix, "computers", "list"],
    ]


def test_running_service_is_restored_after_independent_edit(tmp_path, monkeypatch):
    cfg = base(tmp_path)
    monkeypatch.setattr(
        "acp_gateway.cli.wizard._managed_running", lambda path, role: role == "gateway"
    )
    monkeypatch.setattr("acp_gateway.cli.wizard._ensure_idle", lambda *a: None)
    calls = []
    monkeypatch.setattr("acp_gateway.service.control", lambda action, **kw: calls.append(action))
    wizard(
        tmp_path,
        section="logging",
        prompts=save_answers(
            **{
                "Stop the running service before applying settings?": True,
                "Logging: level": "DEBUG",
            }
        ),
    )
    assert calls == ["stop", "start"]
    assert load_config(cfg.config_path, cfg.env_file).settings.logging.level == "DEBUG"


def test_section_restart_failure_restores_files_and_previous_service(tmp_path, monkeypatch):
    cfg = base(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    monkeypatch.setattr(
        "acp_gateway.cli.wizard._managed_running", lambda path, role: role == "gateway"
    )
    monkeypatch.setattr("acp_gateway.cli.wizard._ensure_idle", lambda *a: None)
    calls = []

    def control(action, **kwargs):
        calls.append(action)
        if calls == ["stop", "start"]:
            raise ValueError("new configuration did not start")

    monkeypatch.setattr("acp_gateway.service.control", control)
    with pytest.raises(ValueError, match="did not start"):
        wizard(
            tmp_path,
            section="logging",
            prompts=save_answers(
                **{
                    "Stop the running service before applying settings?": True,
                    "Logging: level": "DEBUG",
                }
            ),
        )
    assert calls == ["stop", "start", "stop", "start"]
    assert all(p.read_bytes() == data for p, data in before.items())
    assert load_config(cfg.config_path, cfg.env_file).settings.logging.level == "INFO"


def test_server_enrollment_is_an_explicit_independent_step(tmp_path):
    import yaml

    from acp_gateway.storage.db import Store

    cfg = base(tmp_path)
    raw = yaml.safe_load(cfg.config_path.read_text())
    raw["data_dir"] = str(tmp_path / "data")
    cfg.config_path.write_text(yaml.safe_dump(raw))
    setup_menu(
        tmp_path,
        prompts=Sections(
            ["computers", "exit"],
            **{
                "Computers": "enroll",
                "Computer ID": "home",
            },
        ),
    )
    key = tmp_path / "enrollment/home.key"
    assert key.is_file()
    store = Store.open_in(tmp_path / "data")
    try:
        assert store.computers.authenticate("home", key.read_text().strip())
    finally:
        store.close()
    assert load_config(cfg.config_path, cfg.env_file).settings.agents == []


def test_active_service_role_change_is_refused_without_writing(tmp_path, monkeypatch):
    base(tmp_path, "local")
    before = (tmp_path / STATE).read_bytes()
    monkeypatch.setattr(
        "acp_gateway.cli.wizard._managed_running", lambda path, role: role == "gateway"
    )
    with pytest.raises(ValueError, match="Stop the existing service"):
        wizard(tmp_path, section="base", role="server", prompts=save_answers())
    assert (tmp_path / STATE).read_bytes() == before


@pytest.mark.parametrize(
    "role,hidden",
    [
        ("server", {"agents", "connection", "hermes", "agent-tools"}),
        ("connector", {"gateway", "telegram", "hermes", "computers", "agent-tools"}),
    ],
)
def test_role_specific_setup_choices(tmp_path, role, hidden):
    base(tmp_path, role)

    class RoleChoices(Sections):
        def choice(self, label, choices, default):
            if label == "Setup section":
                assert hidden.isdisjoint(choices)
                assert {"base", "service", "check", "exit"}.issubset(choices)
            return super().choice(label, choices, default)

    assert setup_menu(tmp_path, prompts=RoleChoices(["exit"])) == 0


def test_local_working_directory_is_real_and_absolute(tmp_path):
    from acp_gateway.cli.wizard import _working_directory

    assert _working_directory(str(tmp_path)) == str(tmp_path)
    for value in ("relative", str(tmp_path / "missing")):
        with pytest.raises(ValueError, match="existing absolute"):
            _working_directory(value)


def test_connection_edit_warns_to_reinstall_service_with_selected_paths(tmp_path, capsys):
    base(tmp_path, "connector")
    key = tmp_path / "home.key"
    key.write_text("acpc_" + "a" * 43)
    key.chmod(0o600)
    answers = {
        "Enrollment key file copied privately from the server": str(key),
        "Server connection URL": "wss://first.example/acpgw/connect",
    }
    wizard(tmp_path, section="connection", prompts=save_answers(**answers))
    capsys.readouterr()
    answers["Server connection URL"] = "wss://second.example/acpgw/connect"
    wizard(tmp_path, section="connection", prompts=save_answers(**answers))
    output = capsys.readouterr().out
    assert "enable/start does not refresh" in output
    assert "service --role connector install" in output
    assert str(tmp_path / ".env") in output
    assert parse((tmp_path / STATE).read_bytes())["connection"]["dispatcher_url"].startswith(
        "wss://second.example/"
    )


def test_setup_persists_custom_env_selection(tmp_path):
    selected = tmp_path / "private" / "custom.env"
    wizard(tmp_path, env_file=selected, section="base", role="connector", prompts=save_answers())
    assert parse((tmp_path / STATE).read_bytes())["env_file"] == str(selected)


def test_updates_section_is_explicit_and_does_not_start_services(tmp_path, monkeypatch):
    cfg = base(tmp_path)
    assert cfg.settings.updates.enabled is False
    secrets = (tmp_path / ".env").read_bytes()
    monkeypatch.setattr(
        "acp_gateway.updater_service.manage", lambda *a: pytest.fail("service action")
    )
    wizard(
        tmp_path,
        section="updates",
        prompts=save_answers(
            **{
                "Updates: enabled": True,
                "Updates: spool dir": str(tmp_path / "private-spool"),
            }
        ),
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.settings.updates.enabled is True
    assert cfg.settings.updates.spool_dir == tmp_path / "private-spool"
    assert (tmp_path / ".env").read_bytes() == secrets


def test_setup_updater_status_routes_to_selected_config_and_policy(tmp_path):
    base(tmp_path, "connector")
    calls = []
    setup_menu(
        tmp_path,
        prompts=Sections(["updater", "exit"]),
        execute=lambda args: calls.append(args) or 0,
    )
    assert calls == [
        [
            "--config",
            str(tmp_path / "config.yaml"),
            "--env-file",
            str(tmp_path / ".env"),
            "updater",
            "--policy",
            str(tmp_path / "updater.json"),
            "status",
        ]
    ]
