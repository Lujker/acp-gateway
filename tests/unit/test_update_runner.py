"""Local companion policy provisioning, without starting a worker or uv maintenance."""

import json
import subprocess

import pytest
from test_saved_connector_launch import saved as saved

from acp_gateway import installation, update_runner
from acp_gateway.cli.main import main


@pytest.fixture
def provision(saved, tmp_path, monkeypatch):
    config, _ = saved
    env = tmp_path / "private.env"
    env.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = []

    def run(command, **kw):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, str(bin_dir), "")

    monkeypatch.setattr(
        installation, "installation_info", lambda: {"manager": "uv", "source": "registry"}
    )
    monkeypatch.setattr(installation, "_uv", lambda: "/fake/uv")
    monkeypatch.setattr(installation, "lease_path", lambda: tmp_path / "installation.lock")
    monkeypatch.setattr(installation.paths, "data_dir", lambda: tmp_path / "platform-data")
    monkeypatch.setattr(update_runner.subprocess, "run", run)
    return config, env, calls


def test_init_policy_records_exact_paths_and_no_secrets_or_start(provision, tmp_path):
    config, env, calls = provision
    policy = tmp_path / "updater.json"
    assert (
        main(
            [
                "--config",
                str(config),
                "--env-file",
                str(env),
                "updater",
                "--policy",
                str(policy),
                "--init",
                "--role",
                "connector",
            ]
        )
        == 0
    )
    value = json.loads(policy.read_text())
    assert value["auto_refresh"] is True
    assert value["target"] == "home" and value["role"] == "connector"
    assert value["config"] == str(config) and value["env_file"] == str(env)
    assert value["result_dir"] == str(tmp_path / "platform-data/updates/installation")
    assert "acpc_" not in policy.read_text()
    assert calls == [["/fake/uv", "tool", "dir", "--bin"]]
    original = policy.read_bytes()
    with pytest.raises(FileExistsError):
        update_runner.init_policy(policy, config, env, role="connector")
    assert policy.read_bytes() == original


def test_non_registry_installation_cannot_provision_native_updater(
    provision, tmp_path, monkeypatch
):
    config, env, calls = provision
    monkeypatch.setattr(
        installation, "installation_info", lambda: {"manager": "unmanaged", "source": "editable"}
    )
    policy = tmp_path / "updater.json"
    with pytest.raises(ValueError, match="registry"):
        update_runner.init_policy(policy, config, env, role="connector")
    assert not policy.exists() and not calls
