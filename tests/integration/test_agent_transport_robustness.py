"""Malformed agent traffic must yield normalized errors and a client that recovers."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from acp_gateway.agents import AgentClient, AgentError, AgentUnavailable, TransportDisconnected
from acp_gateway.agents.transport import PinnedWebSocketTransport
from acp_gateway.config import AgentProfile

INIT_OK = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
DEEPLY_NESTED = "[" * 100_000 + "]" * 100_000


class FakeAgent:
    """Answers ``initialize`` and ``session/new``.

    ``junk`` frames are sent before the ``session/new`` answer, while the client
    has a request pending; ``$ID`` in a frame is replaced with that request id.
    """

    def __init__(self, *, init_result=INIT_OK, junk=(), junk_connections=None, answer=True):
        self.init_result = init_result
        self.junk = junk
        self.junk_connections = junk_connections  # None: send junk on every connection
        self.answer = answer
        self.session_result = {"sessionId": "s1"}
        self.connections = []
        self.initialize_seen = asyncio.Event()
        self.headers = []

    async def handler(self, ws):
        self.connections.append(ws)
        self.headers.append(ws.request.headers)
        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get("method")
            if method == "initialize":
                self.initialize_seen.set()
                if not self.answer:
                    continue
                await self._reply(ws, msg, self.init_result)
            elif method == "session/new":
                limit = self.junk_connections
                if limit is None or len(self.connections) <= limit:
                    for frame in self.junk:
                        await ws.send(frame.replace("$ID", str(msg["id"])))
                await self._reply(ws, msg, self.session_result)

    @staticmethod
    async def _reply(ws, msg, result):
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}))


def client_for(port: int, tmp_path, **kwargs) -> AgentClient:
    profile = AgentProfile(
        alias="p", kind="generic", url=f"ws://127.0.0.1:{port}/acp", default_cwd="/"
    )
    options = {"pin_dir": tmp_path / "pins", "reconnect_delays": (0.01,), "request_timeout": 5}
    return AgentClient(profile, **(options | kwargs))


async def raw_server(reply: bytes):
    """A TCP server that answers the WebSocket upgrade with ``reply`` and hangs up."""

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


# ------------------------------------------------------------ receive loop


@pytest.mark.parametrize(
    "frame",
    [
        "[1, 2]",
        '"just a string"',
        '{"jsonrpc": "2.0", "id": [1], "result": {}}',
        '{"jsonrpc": "2.0", "id": {"a": 1}, "result": {}}',
        '{"jsonrpc": "2.0", "id": $ID, "error": "boom"}',
        '{"jsonrpc": "2.0", "id": 7, "method": 5}',
        DEEPLY_NESTED,
        "not json",
    ],
    ids=[
        "batch",
        "string",
        "list-id",
        "object-id",
        "string-error",
        "numeric-method",
        "deep-nesting",
        "not-json",
    ],
)
async def test_invalid_frames_are_skipped(tmp_path, frame):
    agent = FakeAgent(junk=[frame])
    async with serve(agent.handler, "127.0.0.1", 0, max_size=None) as server:
        client = client_for(server.sockets[0].getsockname()[1], tmp_path)
        try:
            await client.ensure_connected()
            assert await client.new_session() == "s1"
            assert client.connected
        finally:
            await client.close()
    assert len(agent.connections) == 1


async def test_dead_receive_loop_is_dropped_and_reconnected(tmp_path, monkeypatch):
    # Let a batch frame through to the SDK, which stops reading while the socket stays open.
    monkeypatch.setattr("acp_gateway.agents.transport._acceptable", lambda message: True)
    agent = FakeAgent(junk=["[1, 2]"], junk_connections=1)
    async with serve(agent.handler, "127.0.0.1", 0) as server:
        client = client_for(server.sockets[0].getsockname()[1], tmp_path)
        try:
            await client.ensure_connected()
            with pytest.raises(TransportDisconnected):
                await client.new_session()
            assert not client.connected
            assert await client.new_session() == "s1"
        finally:
            await client.close()
    assert len(agent.connections) == 2


# --------------------------------------------------------------- handshake


@pytest.mark.parametrize(
    "reply",
    [
        b"HTTP/1.1 101 Switching Protocols\r\n\r\n",
        b"NOT HTTP\r\n\r\n",
        b"",
    ],
    ids=["101-without-upgrade", "garbage-status-line", "close-immediately"],
)
async def test_failed_handshake_is_normalized_and_retried(tmp_path, reply):
    server, port = await raw_server(reply)
    try:
        with pytest.raises(AgentUnavailable, match="WebSocket handshake with the agent failed"):
            await PinnedWebSocketTransport.connect(
                f"ws://127.0.0.1:{port}/acp", headers={}, pin=None, open_timeout=3
            )
        with pytest.raises(AgentUnavailable, match="is unavailable"):
            await client_for(port, tmp_path).ensure_connected()
    finally:
        server.close()
        await server.wait_closed()


async def test_environment_proxy_is_not_used(monkeypatch):
    proxied = []

    async def proxy(reader, writer):
        proxied.append(await reader.read(4096))
        writer.close()

    proxy_server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    proxy_url = f"http://127.0.0.1:{proxy_server.sockets[0].getsockname()[1]}"
    for name in ("http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
        monkeypatch.setenv(name.upper(), proxy_url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)

    agent = FakeAgent()
    try:
        async with serve(agent.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            transport = await PinnedWebSocketTransport.connect(
                f"ws://127.0.0.1:{port}/acp",
                headers={"X-Secret-Key": "test-agent-secret"},
                pin=None,
                open_timeout=3,
            )
            await transport.close()
    finally:
        proxy_server.close()
        await proxy_server.wait_closed()
    assert proxied == []
    assert agent.headers[0]["X-Secret-Key"] == "test-agent-secret"


# -------------------------------------------------------------- initialize


async def test_invalid_initialize_result_is_normalized_and_closed(tmp_path):
    agent = FakeAgent(init_result={"protocolVersion": "x"})
    async with serve(agent.handler, "127.0.0.1", 0) as server:
        client = client_for(server.sockets[0].getsockname()[1], tmp_path)
        with pytest.raises(AgentUnavailable, match="invalid initialize response"):
            await client.ensure_connected()
        assert not client.connected
        for ws in agent.connections:
            await asyncio.wait_for(ws.wait_closed(), timeout=5)
    assert len(agent.connections) == 2  # the first attempt and one retry


async def test_cancelled_handshake_closes_the_connection(tmp_path):
    agent = FakeAgent(answer=False)
    async with serve(agent.handler, "127.0.0.1", 0) as server:
        client = client_for(server.sockets[0].getsockname()[1], tmp_path)
        task = asyncio.create_task(client.ensure_connected())
        await asyncio.wait_for(agent.initialize_seen.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (ws,) = agent.connections
        await asyncio.wait_for(ws.wait_closed(), timeout=5)
        assert not client.connected


async def test_invalid_response_is_an_agent_error(tmp_path):
    agent = FakeAgent()
    agent.session_result = {"unexpected": True}
    async with serve(agent.handler, "127.0.0.1", 0) as server:
        client = client_for(server.sockets[0].getsockname()[1], tmp_path)
        try:
            with pytest.raises(AgentError, match="invalid response"):
                await client.new_session()
            assert client.connected  # a bad answer is not a broken connection
        finally:
            await client.close()
