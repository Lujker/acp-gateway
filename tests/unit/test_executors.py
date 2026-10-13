from types import SimpleNamespace

import httpx
import pytest
from dotenv import dotenv_values
from test_wizard import save_answers

from acp_gateway.cli import executors as ex
from acp_gateway.cli.main import main
from acp_gateway.cli.wizard import wizard
from acp_gateway.config import load_config


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setattr("acp_gateway.cli.wizard._managed_running", lambda *args: False)
    monkeypatch.setattr("acp_gateway.cli.wizard._check_ports", lambda *args: None)
    monkeypatch.setattr(ex.shutil, "which", lambda *args: None)
    wizard(tmp_path, prompts=save_answers(**{"Prepare a managed local Goose listener?": True}))
    return load_config(tmp_path / "config.yaml", tmp_path / ".env")


def test_managed_listener_passes_key_only_through_environment(managed, monkeypatch):
    calls = []
    monkeypatch.setattr(ex, "goose_binary", lambda: "/tools/goose")
    monkeypatch.setattr(ex, "_call", lambda command, **kwargs: calls.append((command, kwargs)) or 0)
    assert ex.serve(managed, "goose") == 0
    command, kwargs = calls[0]
    secret = managed.secrets.get("AGENT_GOOSE_SECRET").get_secret_value()
    assert secret == kwargs["env"]["GOOSE_SERVER__SECRET_KEY"]
    assert secret not in " ".join(command)
    assert command == ["/tools/goose", "serve", "--host", "127.0.0.1", "--port", "3284"]


def test_goose_service_keeps_secrets_out_of_unit(managed, tmp_path, monkeypatch):
    from acp_gateway import service

    monkeypatch.setattr(ex.sys, "platform", "linux")
    monkeypatch.setattr(ex, "goose_binary", lambda: "/tools/goose")
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "units" / "gateway.service")
    commands = []
    monkeypatch.setattr(service, "_systemctl", lambda *args, **kwargs: commands.append(args) or "")
    assert ex.service(managed, "goose", "install") == 0
    unit = tmp_path / "units" / "acpgw-goose-goose.service"
    contents = unit.read_text()
    assert '"agents" "serve" "goose"' in contents
    assert "PATH=/tools:" in contents
    assert all(v not in contents for v in dotenv_values(managed.env_file).values())
    unit.write_text("unmanaged")
    with pytest.raises(ValueError, match="unmanaged"):
        ex.service(managed, "goose", "uninstall")


@pytest.mark.parametrize("windows", [False, True])
def test_official_installer_plan_is_pinned_and_isolated(tmp_path, monkeypatch, windows):
    monkeypatch.setattr(ex.sys, "platform", "win32" if windows else "linux")
    monkeypatch.setattr(ex.shutil, "which", lambda name: "/runner")
    monkeypatch.setattr(ex, "bin_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(ex, "goose_binary", lambda: str(tmp_path / "tools" / "goose"))
    monkeypatch.setattr(
        ex.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200, content=b"official", request=httpx.Request("GET", url)
        ),
    )
    calls = []
    monkeypatch.setattr(ex, "_call", lambda command, **kwargs: calls.append((command, kwargs)) or 0)
    assert (
        ex.run_bootstrap(
            SimpleNamespace(agent_command="install", version="1.53.0", dry_run=False, yes=True)
        )
        == 0
    )
    command, options = calls[0]
    assert options["env"]["CONFIGURE"] == "false"
    assert options["env"]["GOOSE_VERSION"] == "1.53.0"
    assert options["env"]["GOOSE_BIN_DIR"] == str(tmp_path / "tools")
    assert command[0] == "/runner"
    assert ("-File" in command) == windows


def test_installer_dry_run_does_not_need_config_or_network(monkeypatch):
    monkeypatch.setattr(ex.httpx, "get", lambda *args, **kwargs: pytest.fail("network"))
    assert main(["agents", "install", "goose", "--dry-run"]) == 0


def test_goose_service_stop_works_with_broken_config(tmp_path, monkeypatch):
    from acp_gateway import service

    config = tmp_path / "config.yaml"
    config.write_text("broken: [yaml")
    unit = tmp_path / "acpgw-goose-goose.service"
    unit.write_text(service.GOOSE_MARKER + service._quote(str(config)))
    monkeypatch.setattr(ex.sys, "platform", "linux")
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "gateway.service")
    commands = []
    monkeypatch.setattr(service, "_systemctl", lambda *args, **kwargs: commands.append(args) or "")
    assert main(["--config", str(config), "agents", "service", "goose", "stop"]) == 0
    assert commands == [("stop", unit.name)]


def test_maintenance_guards_and_removes_owned_goose_units(tmp_path, monkeypatch):
    import sys

    from acp_gateway import installation, service

    monkeypatch.setattr(installation.sys, "platform", "linux")
    monkeypatch.setattr(service, "unit_path", lambda role="gateway": tmp_path / f"{role}.service")
    unit = tmp_path / "acpgw-goose-goose.service"
    unit.write_text(
        service.GOOSE_MARKER
        + "ExecStart="
        + service._quote(sys.executable)
        + ' "-m" "acp_gateway"\n'
    )
    monkeypatch.setattr(service, "_systemctl", lambda *args, **kwargs: "active")
    with pytest.raises(ValueError, match="agents service goose stop"):
        installation._service_preflight(uninstall=True)
    assert unit.is_file()
    monkeypatch.setattr(service, "_systemctl", lambda *args, **kwargs: "inactive")
    installation._service_preflight(uninstall=True)
    assert not unit.exists()
