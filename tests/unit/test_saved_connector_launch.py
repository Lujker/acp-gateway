import json

import pytest
import yaml

from acp_gateway.cli.main import main
from acp_gateway.config import load_config
from acp_gateway.connectors.launch import ConnectorLaunch
from acp_gateway.setup_state import STATE, parse


@pytest.fixture
def saved(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "alias": "hermes",
                        "kind": "hermes",
                        "backend": "hermes",
                        "default_cwd": str(tmp_path),
                    }
                ]
            }
        )
    )
    key = tmp_path / "home.key"
    key.write_text("acpc_" + "a" * 43)
    key.chmod(0o600)
    state = {
        "version": 1,
        "role": "connector",
        "connection": {
            "dispatcher_url": "wss://dispatcher.example/acpgw/connect",
            "computer_id": "home",
            "token_file": str(key),
            "tls_fingerprint": "ab" * 32,
        },
    }
    (tmp_path / STATE).write_text(json.dumps(state))
    return config, state


def test_service_install_uses_saved_connection_without_reentering_options(saved, monkeypatch):
    config, state = saved
    seen = []
    monkeypatch.setattr(
        "acp_gateway.service.install", lambda cfg, *, launch: seen.append(launch) or 0
    )
    assert main(["--config", str(config), "service", "--role", "connector", "install"]) == 0
    (launch,) = seen
    assert launch.computer_id == "home"
    assert launch.dispatcher_url == state["connection"]["dispatcher_url"]
    assert str(launch.token_file) == state["connection"]["token_file"]
    assert launch.tls_fingerprint == ":".join(["AB"] * 32)


def test_explicit_launch_is_not_mixed_with_saved_credentials(saved, monkeypatch, capsys):
    config, state = saved
    seen = []
    monkeypatch.setattr(
        "acp_gateway.service.install", lambda cfg, *, launch: seen.append(launch) or 0
    )
    command = ["--config", str(config), "service", "--role", "connector", "install"]
    assert main([*command, "--computer-id", "work"]) == 1
    assert not seen
    assert "connector requires" in capsys.readouterr().err
    assert (
        main(
            [
                *command,
                "--computer-id",
                "work",
                "--dispatcher-url",
                "wss://other.example/acpgw/connect",
                "--token-file",
                state["connection"]["token_file"],
            ]
        )
        == 0
    )
    assert seen[0].computer_id == "work"
    assert seen[0].tls_fingerprint is None  # No hidden reuse of the saved server's pin.


@pytest.mark.parametrize("change", ["role", "relative-key", "url", "pin", "extra", "oversized"])
def test_invalid_saved_connection_is_rejected_before_install(saved, monkeypatch, change, capsys):
    config, state = saved
    if change == "role":
        state["role"] = "server"
    elif change == "relative-key":
        state["connection"]["token_file"] = "home.key"
    elif change == "url":
        state["connection"]["dispatcher_url"] = "wss://user:private-value@example.com/connect"
    elif change == "pin":
        state["connection"]["tls_fingerprint"] = "private-value"
    elif change == "extra":
        state["connection"]["token"] = "private-value"
    contents = json.dumps(state) if change != "oversized" else "private-value" * 65536
    (config.parent / STATE).write_text(contents)
    monkeypatch.setattr(
        "acp_gateway.service.install", lambda *a, **kw: pytest.fail("invalid recipe installed")
    )
    assert main(["--config", str(config), "service", "--role", "connector", "install"]) == 1
    assert "private-value" not in capsys.readouterr().err


def test_missing_saved_role_has_actionable_error(saved):
    config, _ = saved
    (config.parent / STATE).unlink()
    with pytest.raises(ValueError, match="setup --role connector"):
        ConnectorLaunch.from_saved(load_config(config))


@pytest.mark.parametrize("value", [True, "1", 2, None])
def test_saved_role_version_is_an_integer_contract(value):
    with pytest.raises(ValueError, match=r"Invalid setup\.json"):
        parse(json.dumps({"version": value, "role": "local"}))
