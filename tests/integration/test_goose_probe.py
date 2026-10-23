import json
import secrets

import pytest
from websockets.asyncio.server import serve

from acp_gateway.cli.main import main
from acp_gateway.discovery.goose import probe_goose
from fakes.certs import fingerprint, make_cert
from fakes.fake_goose import FakeGooseServer
from fakes.recordings import result_of


async def test_goose_probe_initializes_only_with_recorded_sdk_contract(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    value = secrets.token_urlsafe(32)
    with FakeGooseServer(value) as server:
        row = await probe_goose(server.url("http"), secret=value)
        assert row["status"] == "ready" and row["version"] == "1.53.0"
        assert row["advertised_load_session"] is True
        assert server.log == ["initialize"] and server.sessions == {}
        assert value not in json.dumps(row)
        assert (await probe_goose(server.url("ws"), secret="wrong"))["status"] == "auth-required"


async def test_goose_probe_tls_requires_exact_pin_without_persisting(tmp_path):
    value = secrets.token_urlsafe(32)
    probe_directory = tmp_path / "probe"
    probe_directory.mkdir()
    cert, key = make_cert(probe_directory)
    with FakeGooseServer(value, certfile=cert, keyfile=key) as server:
        url = server.url("wss")
        assert (await probe_goose(url, secret=value))["status"] == "auth-required"
        assert (await probe_goose(url, secret=value, fingerprint="AB" * 32))[
            "status"
        ] == "auth-required"
        assert server.log == []  # pin checked before sending the key
        assert (await probe_goose(url, secret=value, fingerprint=fingerprint(cert)))[
            "status"
        ] == "ready"
        assert server.log == ["initialize"]
    assert sorted(p.name for p in probe_directory.iterdir()) == ["cert-False.pem", "key-False.pem"]


@pytest.mark.parametrize("reply", ["normal", "wrong_agent", "bad_version", "too_large", "timeout"])
async def test_goose_probe_does_not_answer_unsolicited_requests(reply):
    outgoing = []

    async def handler(socket):
        outgoing.append(json.loads(await socket.recv()))
        await socket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "question",
                    "method": "session/request_permission",
                    "params": {"sessionId": "foreign"},
                }
            )
        )
        result = result_of("init", "initialize")
        if reply == "wrong_agent":
            result["agentInfo"]["name"] = "other"
        elif reply == "bad_version":
            result["protocolVersion"] = True
        elif reply == "too_large":
            result["extra"] = "x" * 65536
        if reply != "timeout":
            await socket.send(
                json.dumps({"jsonrpc": "2.0", "id": "acpgw-observer", "result": result})
            )
        async for payload in socket:
            outgoing.append(json.loads(payload))

    async with serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/acp"
        row = await probe_goose(url, timeout=0.2)
    assert row["status"] == (
        "ready"
        if reply == "normal"
        else "offline"
        if reply in {"too_large", "timeout"}
        else "incompatible"
    )
    assert len(outgoing) == 1 and outgoing[0]["method"] == "initialize"
    assert outgoing[0]["params"]["clientCapabilities"]["terminal"] is False


@pytest.mark.parametrize(
    "url",
    [
        "ws://203.0.113.1/acp",
        "ws://localhost/acp",
        "ws://user:password@127.0.0.1/acp",
        "ws://127.0.0.1/acp?token=private",
        "ws://127.0.0.1/acp#fragment",
        "not a URL",
    ],
)
async def test_goose_probe_refuses_untrusted_targets_without_connecting(url, monkeypatch):
    from acp_gateway.discovery import goose

    monkeypatch.setattr(
        goose.PinnedWebSocketTransport, "connect", lambda *a, **k: pytest.fail("network")
    )
    row = await probe_goose(url)
    assert row["status"] == "incompatible" and url not in json.dumps(row)


def test_goose_probe_cli_reports_manual_discovery_and_validates_options(capsys):
    assert main(["agents", "probe", "goose", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["agents"][0]["status"] == "manual-endpoint-required"
    assert main(["agents", "probe", "goose", "--role", "serve"]) == 1
    capsys.readouterr()
    assert main(["agents", "probe", "hermes", "--url", "ws://127.0.0.1/acp"]) == 1


def test_goose_probe_cli_reads_key_from_env_and_hides_it(monkeypatch, capsys):
    value = secrets.token_urlsafe(32)
    monkeypatch.setenv("ACPGW_TEST_PROBE_KEY", value)
    with FakeGooseServer(value) as server:
        assert (
            main(
                [
                    "agents",
                    "probe",
                    "goose",
                    "--url",
                    server.url("ws"),
                    "--secret-env",
                    "ACPGW_TEST_PROBE_KEY",
                ]
            )
            == 0
        )
        text = capsys.readouterr().out
        assert "Goose" in text and "1.53.0" in text and value not in text
        assert server.sessions == {} and server.log == ["initialize"]


async def test_goose_probe_redacts_backend_echo_of_credential():
    value = secrets.token_urlsafe(32)

    async def handler(socket):
        await socket.recv()
        result = result_of("init", "initialize")
        result["agentInfo"]["version"] = value
        await socket.send(json.dumps({"jsonrpc": "2.0", "id": "acpgw-observer", "result": result}))
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/acp"
        row = await probe_goose(url, secret=value)
    assert row["status"] == "ready"
    assert value not in json.dumps(row)
