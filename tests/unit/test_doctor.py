import json
import secrets
import shutil

import pytest
from test_wizard import save_answers

from acp_gateway.cli.doctor import _dispatcher_probe, doctor
from acp_gateway.cli.main import main
from acp_gateway.cli.wizard import wizard
from fakes.certs import fingerprint, make_cert
from fakes.fake_goose import FakeGooseServer


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr("acp_gateway.cli.wizard._managed_running", lambda *args: False)
    monkeypatch.setattr("acp_gateway.cli.wizard._check_ports", lambda *args: None)
    which = shutil.which
    monkeypatch.setattr(
        "acp_gateway.cli.wizard.shutil.which",
        lambda name: which(name) if name == "powershell.exe" else None,
    )
    wizard(tmp_path, prompts=save_answers())
    return tmp_path / "config.yaml", tmp_path / ".env"


def test_missing_and_invalid_config_are_machine_readable(tmp_path, capsys):
    assert main(["doctor", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["checks"][0]["fix"] == "acpgw setup"
    (tmp_path / "config.yaml").write_text("bad: [yaml")
    assert doctor(tmp_path / "config.yaml", as_json=True) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_offline_doctor_does_not_probe_or_write(configured, monkeypatch, capsys):
    config, env = configured
    capsys.readouterr()
    before = {p: p.read_bytes() for p in config.parent.iterdir() if p.is_file()}
    monkeypatch.setattr(
        "acp_gateway.cli.doctor.httpx.get", lambda *args, **kwargs: pytest.fail("network")
    )
    assert doctor(config, env, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"]
    assert any(c["name"] == "network" for c in report["checks"])
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 61])
def test_bad_timeout_has_json_error(timeout, capsys):
    assert doctor(timeout=timeout, as_json=True) == 1
    assert json.loads(capsys.readouterr().out)["checks"][0]["name"] == "timeout"


def test_acp_live_check_no_session_no_secret_no_pin_mutation(configured, monkeypatch, capsys):
    import yaml

    config, env = configured
    capsys.readouterr()
    secret = secrets.token_urlsafe(32)
    cert, key = make_cert(config.parent)
    with FakeGooseServer(secret, certfile=str(cert), keyfile=str(key)) as fake:
        raw = yaml.safe_load(config.read_text())
        raw["data_dir"] = str(config.parent / "runtime")
        raw["agents"][0].update(url=fake.url("wss"), secret_env="AGENT_TEST_SECRET")
        config.write_text(yaml.safe_dump(raw))
        with env.open("a") as stream:
            stream.write(f"AGENT_TEST_SECRET={secret}\n")
        # The gateway is intentionally stopped; the ACP result is checked separately.
        assert doctor(config, env, live=True, as_json=True, timeout=1) == 1
        output = capsys.readouterr().out
        report = json.loads(output)
        assert secret not in output
        assert any(c["name"] == "agent:goose" and c["status"] == "ok" for c in report["checks"])
        assert not fake.sessions
        assert fake.log == ["initialize"]
        assert not (config.parent / "runtime").exists()


def test_dispatcher_tls_pin_mismatch_fails_without_registration(tmp_path):
    cert, key = make_cert(tmp_path)
    with FakeGooseServer("unused", certfile=str(cert), keyfile=str(key)) as fake:
        _dispatcher_probe(
            {"dispatcher_url": fake.url("wss"), "tls_fingerprint": fingerprint(cert)}, 2
        )
        with pytest.raises(ValueError, match="fingerprint mismatch"):
            _dispatcher_probe({"dispatcher_url": fake.url("wss"), "tls_fingerprint": "00" * 32}, 2)
        assert fake.log == []


def test_doctor_detects_stale_hermes_token(configured, tmp_path, capsys):
    config, env = configured
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("mcp_servers: {}")
    wizard(
        tmp_path,
        section="hermes",
        prompts=save_answers(
            **{
                "Connect this gateway to a local Hermes installation?": True,
                "Hermes configuration directory": str(home),
            }
        ),
    )
    capsys.readouterr()
    assert doctor(config, env, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "hermes" and c["status"] == "ok" for c in report["checks"])
    (home / ".env").write_text("ACPGW_MCP_TOKEN=stale")
    assert doctor(config, env, as_json=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "hermes" and c["status"] == "error" for c in report["checks"])


def test_server_without_manual_profiles_is_valid(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("acp_gateway.cli.wizard._check_ports", lambda *args: None)
    monkeypatch.setattr("acp_gateway.cli.wizard._managed_running", lambda *args: False)
    wizard(tmp_path, section="base", role="server", prompts=save_answers())
    capsys.readouterr()
    assert doctor(tmp_path / "config.yaml", tmp_path / ".env", as_json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "agents" and c["status"] == "info" for c in report["checks"])


def test_partial_connector_explains_missing_connection(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("acp_gateway.cli.wizard._managed_running", lambda *args: False)
    wizard(tmp_path, section="base", role="connector", prompts=save_answers())
    capsys.readouterr()
    assert doctor(tmp_path / "config.yaml", tmp_path / ".env", as_json=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert any(c["detail"] == "Server connection is not configured yet" for c in report["checks"])
