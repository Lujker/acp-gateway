"""Rendezvous rotation and redirect refusal against actual local HTTP/WS."""

import hashlib
import json
import secrets

import pytest
from test_hermes_probe import TOKEN
from test_hermes_probe import backend as backend
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from acp_gateway.cli.probe import probe_hermes
from acp_gateway.discovery.hermes import HermesDiscoveryError, read_endpoint
from acp_gateway.discovery.hermes_rpc import HermesRpcClient


def write_record(directory, endpoint, token):
    record = {
        "role": endpoint.role,
        "pid": endpoint.pid,
        "host": endpoint.host,
        "port": endpoint.port,
        "protocolVersion": 1,
        "profiles": ["default"],
        "tokenFingerprint": hashlib.sha256(token.encode()).hexdigest()[:16],
    }
    for suffix, value in (("json", json.dumps(record)), ("token", token)):
        path = directory / f"host-{endpoint.role}.{suffix}"
        path.write_text(value)
        path.chmod(0o600)


async def test_new_probe_rediscovery_uses_rotated_port_pid_and_token(backend, tmp_path):
    old_endpoint, _, _ = backend
    write_record(tmp_path, old_endpoint, TOKEN)
    assert (await probe_hermes(tmp_path, role="desktop-serve"))[0]["status"] == "ready"
    new_token = secrets.token_urlsafe(32)
    requests = []

    def http(connection, request):
        requests.append(request.path.split("?")[0])
        if request.path.startswith("/api/ws?"):
            assert TOKEN not in request.path
            return None
        if request.headers.get("Authorization") != f"Bearer {new_token}":
            return Response(401, "Unauthorized", Headers(), b"")
        result = (
            {"ok": True, "pid": 5678, "role": "desktop-serve", "protocolVersion": 1}
            if request.path == "/api/host/identity"
            else {"version": "0.21.6"}
        )
        return Response(200, "OK", Headers(), json.dumps(result).encode())

    async def handler(socket):
        async for raw in socket:
            frame = json.loads(raw)
            result = {
                "ping": {"pong": True},
                "gateway.capabilities": {},
                "session.active_list": {"sessions": []},
            }[frame["method"]]
            await socket.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}))

    async with serve(handler, "127.0.0.1", 0, process_request=http) as server:
        from acp_gateway.discovery.hermes import HermesEndpoint

        new_endpoint = HermesEndpoint(
            "desktop-serve",
            5678,
            "127.0.0.1",
            server.sockets[0].getsockname()[1],
            ("default",),
            new_token,
        )
        write_record(tmp_path, new_endpoint, new_token)
        # A torn replacement must fail closed, without dialing with mismatched credentials.
        token_file = tmp_path / "host-desktop-serve.token"
        token_file.write_text(TOKEN)
        with pytest.raises(HermesDiscoveryError, match="do not match"):
            read_endpoint(tmp_path, "desktop-serve")
        assert not requests
        token_file.write_text(new_token)
        row = (await probe_hermes(tmp_path, role="desktop-serve"))[0]
        assert row["status"] == "ready" and row["endpoint"] == new_endpoint.base_url
        assert requests[:2] == ["/api/host/identity", "/api/status"]
        assert requests[-1] == "/api/ws"
        assert TOKEN not in json.dumps(row) and new_token not in json.dumps(row)


@pytest.mark.parametrize("redirect_stage", ["identity", "websocket"])
async def test_authenticated_probe_never_follows_redirects(redirect_stage):
    requests = []
    token = secrets.token_urlsafe(32)

    def http(connection, request):
        path = request.path.split("?")[0]
        requests.append(path)
        if (path == "/api/host/identity" and redirect_stage == "identity") or path == "/api/ws":
            return Response(302, "Found", Headers({"Location": "/redirect-target"}), b"")
        result = (
            {"ok": True, "pid": 123, "role": "serve", "protocolVersion": 1}
            if path == "/api/host/identity"
            else {"version": "0.21.6"}
        )
        return Response(200, "OK", Headers(), json.dumps(result).encode())

    async def handler(socket):
        pytest.fail("redirect unexpectedly reached a WebSocket handler")

    async with serve(handler, "127.0.0.1", 0, process_request=http) as server:
        from acp_gateway.discovery.hermes import HermesEndpoint

        endpoint = HermesEndpoint(
            "serve", 123, "127.0.0.1", server.sockets[0].getsockname()[1], ("default",), token
        )
        with pytest.raises(HermesDiscoveryError) as error:
            await HermesRpcClient.connect(endpoint)
        assert token not in str(error.value)
    assert "/redirect-target" not in requests
