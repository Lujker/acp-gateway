"""Service lifecycle, persistent credentials and systemd escaping."""

import subprocess

import pytest
from dotenv import dotenv_values

from acp_gateway import service
from acp_gateway.cli.main import main
from acp_gateway.config import load_config
from acp_gateway.setup import setup


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        stdout = "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(service.subprocess, "run", run)
    return calls


def test_setup_private_independent_tokens_and_no_overwrite(tmp_path, capsys):
    directory = tmp_path / "new-config"
    assert setup(directory) == 0
    config = directory / "config.yaml"
    env = directory / ".env"
    values = dotenv_values(env)
    assert len(values["ACPGW_API_TOKEN"]) >= 32
    assert values["ACPGW_API_TOKEN"] != values["ACPGW_MCP_TOKEN"]
    assert env.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert values["ACPGW_API_TOKEN"] not in capsys.readouterr().out
    config.write_text("# existing configuration\n")
    before = env.read_bytes()
    assert setup(directory) == 0
    assert env.read_bytes() == before
    assert config.read_text() == "# existing configuration\n"


def test_service_install_control_and_uninstall_keep_configuration(tmp_path, manager):
    assert main(["setup", "--config-dir", str(tmp_path)]) == 0
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert (
        main(
            [
                "--config",
                str(cfg.config_path),
                "--env-file",
                str(cfg.env_file),
                "service",
                "install",
            ]
        )
        == 0
    )
    unit = service.unit_path()
    content = unit.read_text()
    assert content.startswith(service.MARKER)
    assert str(cfg.config_path) in content and str(cfg.env_file) in content
    assert "Restart=on-failure" in content and "UMask=0077" in content
    for secret in dotenv_values(tmp_path / ".env").values():
        if secret:
            assert secret not in content
    assert not any("enable" in call for call in manager)
    assert service.control("enable") == 0
    assert manager[-1][2:] == ["enable", "--now", service.UNIT]
    assert service.control("disable") == 0
    assert manager[-1][2:] == ["disable", "--now", service.UNIT]
    assert service.control("uninstall") == 0
    assert not unit.exists()
    assert cfg.env_file.exists() and cfg.config_path.exists()


def test_status_does_not_load_broken_app_config(tmp_path, manager, capsys):
    (tmp_path / "config.yaml").write_text("not: [valid: config\n")
    assert main(["service", "status"]) == 0
    assert "ActiveState=inactive" in capsys.readouterr().out


def test_unmanaged_or_symlink_unit_is_never_replaced(tmp_path, manager):
    setup(tmp_path)
    unit = service.unit_path()
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/bin/true\n")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    with pytest.raises(ValueError, match="unmanaged"):
        service.install(cfg)
    with pytest.raises(ValueError, match="unmanaged"):
        service.control("uninstall")
    assert manager == []
    unit.unlink()
    unit.symlink_to(tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="unmanaged"):
        service.install(cfg)


def test_failed_manager_does_not_write_unit(tmp_path, manager, monkeypatch):
    setup(tmp_path)

    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "Failed to connect to bus")

    monkeypatch.setattr(service.subprocess, "run", failed)
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    with pytest.raises(ValueError, match="Failed to connect to bus"):
        service.install(cfg)
    assert not service.unit_path().exists()


def test_service_requires_credentials_persisted_in_env_file(tmp_path, manager, monkeypatch):
    setup(tmp_path)
    monkeypatch.setenv("ACPGW_API_TOKEN", "shell-only-credential")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    with pytest.raises(ValueError, match="persist ACPGW_API_TOKEN"):
        service.install(cfg)
    assert manager == []


def test_unit_escapes_paths_and_supports_frozen_executable(tmp_path, monkeypatch):
    config = tmp_path / 'space $HOME %h "quoted"' / "config.yaml"
    unit = service.render_unit(config, config.parent / ".env")
    assert "$$HOME %%h" in unit.split("ExecStart=", 1)[1].splitlines()[0]
    assert "$HOME %%h" in unit.split("WorkingDirectory=", 1)[1].splitlines()[0]
    assert '\\"quoted\\"' in unit
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service.sys, "executable", "/opt/acpgw")
    frozen = service.render_unit(config, None)
    assert 'ExecStart="/opt/acpgw"' in frozen
    assert '"-m"' not in frozen
    with pytest.raises(ValueError, match="control characters"):
        service.render_unit(tmp_path / "bad\npath" / "config.yaml", None)


def test_non_linux_has_clear_error(manager, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    with pytest.raises(ValueError, match="Linux/WSL"):
        service.control("status")
