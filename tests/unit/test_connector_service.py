"""Separate user units, private computer credentials and fatal restart policy."""

from dataclasses import replace

import pytest
from test_service import manager as manager

from acp_gateway import service
from acp_gateway.cli.main import main
from acp_gateway.config import load_config
from acp_gateway.connectors.launch import ConnectorLaunch
from acp_gateway.storage import Store


@pytest.fixture
def computer(tmp_path):
    config, env, key = tmp_path / "computer.yaml", tmp_path / "computer.env", tmp_path / "key"
    config.write_text(
        f"data_dir: {tmp_path / 'local-data'}\n"
        "agents:\n  - alias: goose\n    kind: goose\n"
        "    url: ws://127.0.0.1:3000/acp\n    default_cwd: /work\n"
        "    secret_env: LOCAL_GOOSE_SECRET\n"
    )
    env.write_text("LOCAL_GOOSE_SECRET=mock-local-service-credential\n")
    store = Store.open_in(tmp_path / "registry")
    try:
        store.computers.issue("work", key, display_name="Work")
    finally:
        store.close()
    launch = ConnectorLaunch("ws://192.0.2.1:8766/gateway/connect", "work", key)
    return load_config(config, env), launch


def test_connector_install_and_lifecycle_do_not_touch_gateway_unit(computer, manager, capsys):
    config, launch = computer
    gateway = service.unit_path()
    gateway.parent.mkdir(parents=True)
    gateway.write_text(service.MARKER + "[Service]\nExecStart=/bin/true\n")
    original = gateway.read_bytes()
    assert (
        main(
            [
                "--config",
                str(config.config_path),
                "--env-file",
                str(config.env_file),
                "service",
                "--role",
                "connector",
                "install",
                *launch.arguments()[1:],
            ]
        )
        == 0
    )
    unit = service.unit_path("connector")
    content = unit.read_text()
    assert content.startswith(service.CONNECTOR_MARKER)
    assert '"connector"' in content and '"serve"' not in content
    assert launch.dispatcher_url in content and str(launch.token_file) in content
    assert "RestartPreventExitStatus=2 78" in content
    assert "SuccessExitStatus=130" in content
    assert key_not_leaked(config, launch, content + capsys.readouterr().out)
    assert not any("enable" in args for args in manager)
    for action in ("enable", "disable", "start", "stop", "restart"):
        assert main(["service", "--role", "connector", action]) == 0
        assert manager[-1][-1] == service.CONNECTOR_UNIT
    assert main(["service", "--role", "connector", "uninstall"]) == 0
    assert not unit.exists()
    assert gateway.read_bytes() == original
    assert config.config_path.exists() and config.env_file.exists() and launch.token_file.exists()


def key_not_leaked(config, launch, text):
    credential = launch.token_file.read_text().strip()
    agent = config.secrets.get("LOCAL_GOOSE_SECRET").get_secret_value()
    return credential not in text and agent not in text


@pytest.mark.parametrize("mutation", ["symlink", "public", "bad_url", "missing_options"])
def test_invalid_connector_install_never_writes_or_calls_manager(
    computer, manager, mutation, tmp_path
):
    config, launch = computer
    if mutation == "symlink":
        link = tmp_path / "link"
        link.symlink_to(launch.token_file)
        launch = replace(launch, token_file=link)
    elif mutation == "public":
        launch.token_file.chmod(0o644)
    elif mutation == "bad_url":
        launch = replace(launch, dispatcher_url="ws://user:credential@example.invalid/")
    else:
        launch = replace(launch, computer_id="")
    with pytest.raises((ValueError, OSError)):
        service.install(config, launch=launch)
    assert manager == []
    assert not service.unit_path("connector").exists()


def test_connector_install_requires_persisted_agent_secret_not_owner_token(
    computer, manager, monkeypatch
):
    config, launch = computer
    assert config.secrets.get("ACPGW_API_TOKEN") is None
    monkeypatch.setenv("LOCAL_GOOSE_SECRET", "shell-only-credential")
    config = load_config(config.config_path, config.env_file)
    with pytest.raises(ValueError, match="persist LOCAL_GOOSE_SECRET"):
        service.install(config, launch=launch)
    assert manager == []


def test_connector_status_and_logs_work_with_broken_configuration(tmp_path, manager, capsys):
    (tmp_path / "config.yaml").write_text("not: [valid: yaml\n")
    assert main(["service", "--role", "connector", "status"]) == 0
    assert service.CONNECTOR_UNIT in manager[-1]
    assert "ActiveState=inactive" in capsys.readouterr().out
    assert main(["service", "--role", "connector", "logs"]) == 0
    assert service.CONNECTOR_UNIT in manager[-1]
    assert "--lines=50" in manager[-1]


def test_connector_unit_refuses_unmanaged_or_wrong_role_marker(computer, manager):
    config, launch = computer
    path = service.unit_path("connector")
    path.parent.mkdir(parents=True)
    path.write_text(service.MARKER + "[Service]\nExecStart=/bin/true\n")
    for operation in (
        lambda: service.install(config, launch=launch),
        lambda: service.control("uninstall", role="connector"),
    ):
        with pytest.raises(ValueError, match="unmanaged"):
            operation()
    assert manager == []


def test_connector_render_escapes_paths_and_pin_and_frozen_executable(
    computer, monkeypatch, tmp_path
):
    config, launch = computer
    token = tmp_path / 'key $HOME %h "quoted"'
    launch = replace(
        launch,
        token_file=token,
        dispatcher_url="wss://dispatcher.example/",
        tls_fingerprint=":".join(["AB"] * 32),
    )
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service.sys, "executable", "/opt/acpgw")
    unit = service.render_unit(config.config_path, config.env_file, launch=launch)
    assert 'ExecStart="/opt/acpgw"' in unit and '"-m"' not in unit
    assert "$$HOME %%h" in unit and '\\"quoted\\"' in unit
    assert launch.tls_fingerprint in unit


def test_gateway_install_refuses_connector_flags(computer, manager):
    config, launch = computer
    assert (
        main(
            [
                "--config",
                str(config.config_path),
                "--env-file",
                str(config.env_file),
                "service",
                "install",
                *launch.arguments()[1:],
            ]
        )
        == 1
    )
    assert not service.unit_path().exists() and manager == []
