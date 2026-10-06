"""Runs scripts/spike_acp.py against the fake goose agent.

Validates the spike's own mechanics (transports, auth header, TLS pinning,
permission handling, cancel, load) before it is pointed at the real Work Goose.
"""

import json
import subprocess
from pathlib import Path

import pytest

import spike_acp
from fakes.fake_goose import FakeGooseServer

SECRET = "fake-goose-" + "secret-0042"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("acp_gateway.paths.data_dir", lambda: tmp_path / "data")


def make_cert(tmp_path: Path, *, ca: bool) -> tuple[str, str]:
    cert, key = tmp_path / f"cert-{ca}.pem", tmp_path / f"key-{ca}.pem"
    constraints = "critical,CA:TRUE" if ca else "critical,CA:FALSE"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=fake-goose", "-addext", f"basicConstraints={constraints}",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return str(cert), str(key)


def fingerprint(cert_path: str) -> str:
    result = subprocess.run(
        ["openssl", "x509", "-in", cert_path, "-noout", "-fingerprint", "-sha256"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().split("=", 1)[1]


def write_config(tmp_path: Path, url: str, fp: str = "", secret: str = SECRET) -> list[str]:
    (tmp_path / "config.yaml").write_text(
        "agents:\n"
        "  - alias: work\n"
        "    kind: goose\n"
        f"    url: {url}\n"
        "    secret_env: AGENT_WORK_SECRET\n"
        f'    tls_fingerprint: "{fp}"\n'
        "    default_cwd: /tmp\n"
    )
    (tmp_path / ".env").write_text(f"AGENT_WORK_SECRET={secret}\n")
    return ["--config", str(tmp_path / "config.yaml"), "--env-file", str(tmp_path / ".env")]


def run_spike(*args: str) -> tuple[int, dict, str]:
    code = spike_acp.main(list(args))
    run_dir = sorted(Path("spike-runs").iterdir())[-1]
    summary = json.loads((run_dir / "summary.json").read_text())
    traffic = (
        (run_dir / "traffic.jsonl").read_text() if (run_dir / "traffic.jsonl").exists() else ""
    )
    return code, summary, traffic


@pytest.mark.parametrize("transport", ["ws", "http"])
def test_all_scenarios_plaintext_loopback(tmp_path, transport):
    with FakeGooseServer(SECRET) as server:
        opts = write_config(tmp_path, server.url("ws"))
        code, summary, traffic = run_spike("all", "--transport", transport, *opts)

    if transport == "http":
        # acp 0.12 Streamable HTTP binds a session to the connection only when a result
        # carries sessionId; session/load results don't, so prompting a loaded session
        # gets 404. WebSocket is unaffected. Recorded in road-notes (P0.2).
        assert code == 1
        assert set(summary["errors"]) == {"load"}
        assert "404" in summary["errors"]["load"]
    else:
        assert code == 0, summary.get("errors")
    assert summary["tls"] is None
    assert summary["initialize"]["agentCapabilities"]["loadSession"] is True
    assert [m["id"] for m in summary["modes"]["modes"]["availableModes"]] == [
        "auto",
        "approve",
        "chat",
    ]
    assert summary["ping"]["text"] == "pong"
    assert summary["permission"]["command_ran"] is False
    assert summary["permission"]["requests"][0]["options"][0]["kind"] == "allow_once"
    assert summary["cancel_allow"]["stop_reason"] == "cancelled"
    if transport == "ws":
        assert summary["load"]["remembered"] is True
        assert summary["load"]["replayed_update_kinds"] == {
            "user_message_chunk": 1,
            "agent_message_chunk": 1,
        }
    # Invariants: no client capabilities advertised, no MCP servers passed, secret not recorded.
    assert all(a.client_capabilities.fs.read_text_file is False for a in server.agents)
    assert all(a.client_capabilities.terminal is False for a in server.agents)
    assert "new_session mcp_servers=0" in server.log
    assert SECRET not in traffic


def test_permission_allow_runs_command(tmp_path):
    with FakeGooseServer(SECRET) as server:
        # Base URL without /acp, as entered in Goose Desktop settings.
        opts = write_config(tmp_path, server.url("ws").removesuffix("/acp"))
        code, summary, _ = run_spike("permission", "--permission", "allow", *opts)
    assert code == 0
    assert summary["permission"]["command_ran"] is True


def test_cancel_while_permission_pending(tmp_path):
    with FakeGooseServer(SECRET) as server:
        opts = write_config(tmp_path, server.url("ws"))
        code, summary, _ = run_spike("cancel", "--permission", "hold", *opts)
    assert code == 0
    assert summary["cancel_hold"]["stop_reason"] == "cancelled"
    assert summary["cancel_hold"]["permission_requests"] == 1


def test_auto_mode_skips_permission(tmp_path):
    with FakeGooseServer(SECRET) as server:
        opts = write_config(tmp_path, server.url("ws"))
        code, summary, _ = run_spike("permission", "--mode", "auto", *opts)
    assert code == 0
    assert summary["permission"]["requests"] == []
    assert summary["permission"]["command_ran"] is True


def test_wrong_secret_is_rejected(tmp_path):
    with FakeGooseServer(SECRET) as server:
        opts = write_config(tmp_path, server.url("ws"), secret="wrong-" + "secret-value")
        code, summary, _ = run_spike("init", *opts)
    assert code == 1
    assert "init" in summary["errors"]


@pytest.mark.parametrize("ca", [True, False], ids=["ca-cert", "leaf-cert"])
@pytest.mark.parametrize("transport", ["ws", "http"])
def test_tls_tofu_then_saved_pin(tmp_path, ca, transport):
    cert, key = make_cert(tmp_path, ca=ca)
    with FakeGooseServer(SECRET, certfile=cert, keyfile=key) as server:
        opts = write_config(tmp_path, server.url("wss"))
        code, first, _ = run_spike("ping", "--transport", transport, *opts)
        assert code == 0, first.get("errors")
        assert first["tls"] == {"fingerprint": fingerprint(cert), "source": "tofu-new"}
        assert first["ping"]["text"] == "pong"

        code, second, _ = run_spike("ping", "--transport", transport, *opts)
        assert code == 0
        assert second["tls"]["source"] == "tofu-saved"


def test_tls_configured_pin(tmp_path):
    cert, key = make_cert(tmp_path, ca=False)
    with FakeGooseServer(SECRET, certfile=cert, keyfile=key) as server:
        opts = write_config(tmp_path, server.url("wss"), fp=fingerprint(cert))
        code, summary, _ = run_spike("ping", *opts)
    assert code == 0
    assert summary["tls"]["source"] == "config"


def test_tls_pin_mismatch_refuses_to_connect(tmp_path):
    cert, key = make_cert(tmp_path, ca=False)
    with FakeGooseServer(SECRET, certfile=cert, keyfile=key) as server:
        opts = write_config(tmp_path, server.url("wss"), fp="AB" * 32)
        code, summary, _ = run_spike("ping", *opts)
    assert code == 1
    assert "fingerprint mismatch" in summary["errors"]["ping"]
    assert server.log == []  # nothing reached the agent, the secret was never sent
