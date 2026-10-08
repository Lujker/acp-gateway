"""Computer CLI process and actual owner HTTP against serve's WS ingress."""

import socket
import subprocess
import sys
import time

import httpx
import pytest
import yaml
from test_daemon_cli import daemon

from acp_gateway.config import load_config
from acp_gateway.storage import Store
from fakes.fake_goose import FakeGooseServer


@pytest.mark.parametrize("stop", ["revoke", "sigterm"])
def test_computer_cli_relays_prompt_to_shared_daemon_runtime(tmp_path, stop):
    secret = "mock-" + "process-local-agent-credential"
    owner = "mock-process-owner-credential"
    with FakeGooseServer(secret) as goose:
        # Reserve owner HTTP's socket; the existing daemon helper passes it to uvicorn.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            ingress_port = reservation.getsockname()[1]
        vps = tmp_path / "vps.yaml"
        vps.write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(tmp_path / "vps-data"),
                    "gateway": {"port": sock.getsockname()[1]},
                    "connector": {
                        "enabled": True,
                        "port": ingress_port,
                        "connect_path": "/gateway/connect",
                    },
                    "agents": [
                        {
                            "alias": "goose",
                            "backend": "connector",
                            "computer_id": "work",
                            "kind": "goose",
                            "default_cwd": "/work",
                        },
                        {
                            "alias": "other",
                            "backend": "connector",
                            "computer_id": "work",
                            "default_cwd": "/work",
                        },
                    ],
                }
            )
        )
        vps_env = tmp_path / "vps.env"
        vps_env.write_text(f"ACPGW_API_TOKEN={owner}\n")
        computer = tmp_path / "computer.yaml"
        computer.write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(tmp_path / "computer-data"),
                    "agents": [
                        {
                            "alias": "goose",
                            "kind": "goose",
                            "url": goose.url("ws"),
                            "secret_env": "LOCAL_GOOSE_SECRET",
                            "default_cwd": "/work",
                        }
                    ],
                }
            )
        )
        local_env = tmp_path / "computer.env"
        local_env.write_text(f"LOCAL_GOOSE_SECRET={secret}\n")
        key = tmp_path / "computer.key"
        registry = Store.open_in(tmp_path / "vps-data")
        try:
            registry.computers.issue("work", key, display_name="Work")
        finally:
            registry.close()
        config = load_config(vps, vps_env)
        with (
            daemon(config, sock),
            httpx.Client(
                base_url=f"http://127.0.0.1:{config.settings.gateway.port}",
                trust_env=False,
                headers={"Authorization": f"Bearer {owner}"},
                timeout=10,
            ) as api,
            (tmp_path / "connector.log").open("w+") as log,
        ):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "acp_gateway",
                    "--config",
                    str(computer),
                    "--env-file",
                    str(local_env),
                    "connector",
                    "--computer-id",
                    "work",
                    "--token-file",
                    str(key),
                    "--dispatcher-url",
                    f"ws://127.0.0.1:{ingress_port}/gateway/connect",
                ],
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    assert process.poll() is None, "connector process failed"
                    computers = api.get("/health").json()["computers"]
                    if computers and computers[0]["connected"]:
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError("computer did not register")
                diagnostics = api.get("/computers").json()
                assert diagnostics["computers"][0]["connected_at"]
                routes = {r["address"]: r for r in diagnostics["routes"]}
                assert routes["work/goose"]["status"] == "not_initialized"
                assert routes["work/other"]["status"] == "unadvertised"
                # Diagnostics never connect to a local agent or disclose credentials.
                assert not goose.sessions
                for text, expected in [
                    ("code word process", "OK"),
                    ("What was the code word", "process"),
                ]:
                    response = api.post(
                        "/messages", json={"agent": "work/goose", "text": text, "wait": 5}
                    )
                    assert response.status_code == 202, response.text
                    assert response.json()["status"] == "completed"
                    assert response.json()["answer"] == expected
                routes = api.get("/computers").json()["routes"]
                assert next(r for r in routes if r["address"] == "work/goose")["agent_ready"]
                if stop == "revoke":
                    # Admin commands use another SQLite connection; daemon detects revoke.
                    registry = Store.open_in(tmp_path / "vps-data")
                    try:
                        registry.computers.revoke("work")
                    finally:
                        registry.close()
                    assert process.wait(timeout=5) == 78
                else:
                    process.terminate()
                    assert process.wait(timeout=5) == 0
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    diagnostics = api.get("/computers").json()
                    if not diagnostics["computers"][0]["connected"]:
                        break
                    time.sleep(0.02)
                row = diagnostics["computers"][0]
                assert not row["connected"] and row["disconnected_at"]
                assert row["disconnect_reason"] == (
                    "access_changed" if stop == "revoke" else "connection_closed"
                )
                assert all(r["status"] == "offline" for r in diagnostics["routes"])
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "acp_gateway",
                        "--config",
                        str(vps),
                        "--env-file",
                        str(vps_env),
                        "computers",
                        "status",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 0, result.stderr
                assert '"disconnect_reason"' in result.stdout and '"work/goose"' in result.stdout
                log.seek(0)
                output = log.read()
                assert "computer connected" in output
                assert all(
                    value not in output + result.stdout
                    for value in (secret, owner, key.read_text().strip())
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
