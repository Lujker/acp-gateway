"""AgentClient's ACP lifecycle must work without a direct WebSocket endpoint."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp.agent.connection import AgentSideConnection
from pydantic import SecretStr

from acp_gateway.agents import AgentClient, AgentUnavailable, TransportDisconnected
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.policy import LocalAgentPolicy, PolicyTransport
from fakes.fake_goose import FakeGooseAgent


class MemoryTransport:
    """Two message queues with explicit stream closure, like a demultiplexed relay."""

    def __init__(self, inbox, outbox):
        self.inbox = inbox
        self.outbox = outbox
        self.closed = asyncio.Event()

    async def send(self, message):
        if self.closed.is_set():
            raise ConnectionError("stream closed")
        self.outbox.put_nowait(message)

    async def receive(self):
        message = await self.inbox.get()
        if message is None:
            self.closed.set()
        return message

    async def close(self):
        if not self.closed.is_set():
            self.closed.set()
            self.outbox.put_nowait(None)
            self.inbox.put_nowait(None)


@pytest.fixture(params=[False, True], ids=["plain", "local-policy"])
async def harness(tmp_path, monkeypatch, request):
    # A real WSS-looking profile must never trigger probing or send its secret
    # when the factory supplies the stream. Routing/auth belong to the factory.
    probe = AsyncMock(side_effect=AssertionError("direct TLS probe used"))
    direct = AsyncMock(side_effect=AssertionError("direct WebSocket used"))
    monkeypatch.setattr("acp_gateway.agents.client.pin_certificate", probe)
    monkeypatch.setattr("acp_gateway.agents.client.PinnedWebSocketTransport.connect", direct)
    state = SimpleNamespace(sessions={}, log=[], agents=[], connections=[], streams=[])
    profile = AgentProfile(
        alias="work", kind="goose", url="wss://unused.invalid/acp", default_cwd="/local/work"
    )

    async def factory():
        upstream, downstream = asyncio.Queue(), asyncio.Queue()
        local = MemoryTransport(downstream, upstream)
        remote = MemoryTransport(upstream, downstream)
        agent = FakeGooseAgent(None, state.sessions, state.log)
        connection = AgentSideConnection(agent, remote)
        agent._conn = connection
        state.agents.append(agent)
        state.connections.append(connection)
        state.streams.append(local)
        return (
            PolicyTransport(local, LocalAgentPolicy.from_profile(profile))
            if request.param
            else local
        )

    state.factory = factory
    state.client = AgentClient(
        profile,
        SecretStr("local-" + "agent-credential"),
        pin_dir=tmp_path / "pins",
        transport_factory=factory,
        reconnect_delays=(),
        request_timeout=1,
    )
    try:
        yield state
    finally:
        await state.client.close()
        for connection in state.connections:
            await connection.close()
    probe.assert_not_awaited()
    direct.assert_not_awaited()


@pytest.mark.parametrize("approve", [False, True])
async def test_injected_stream_preserves_sessions_and_human_permissions(harness, approve):
    client = harness.client
    decisions = []

    async def decide(request):
        decisions.append(request)
        return request.option("allow_once" if approve else "reject_once").option_id

    client.set_permission_handler(decide)
    session = await client.new_session("/local/work")
    agent = harness.agents[0]
    assert not agent.client_capabilities.fs.read_text_file
    assert not agent.client_capabilities.fs.write_text_file
    assert not agent.client_capabilities.terminal
    assert agent.mcp_servers_seen == [0]
    assert harness.sessions[session].cwd == "/local/work"
    assert client.tls_pin is None
    await client.ask(session, "code word transport")
    assert (await client.ask(session, "What was the code word")).text == "transport"
    reply = await client.ask(session, "run exactly: echo approved .")
    assert reply.text == ("approved" if approve else "DENIED")
    assert len(decisions) == 1
    assert decisions[0].session_id == session


async def test_injected_stream_reconnect_loads_session_without_replaying_prompt(harness):
    client = harness.client
    session = await client.new_session("/local/work")
    await client.ask(session, "code word retained")
    await harness.connections[0].close()
    await asyncio.wait_for(harness.streams[0].closed.wait(), timeout=1)
    assert not client.connected
    assert (await client.ask(session, "What was the code word")).text == "retained"
    assert len(harness.streams) == 2
    assert harness.log.count("load_session") == 1
    assert harness.sessions[session].history == [
        ("user", "code word retained"),
        ("agent", "OK"),
        ("user", "What was the code word"),
        ("agent", "retained"),
    ]
    assert harness.agents[1].mcp_servers_seen == [0]


async def test_injected_stream_cancel_rejects_pending_permission(harness):
    pending = asyncio.Event()

    async def decide(request):
        pending.set()
        await asyncio.Event().wait()

    client = harness.client
    client.set_permission_handler(decide)
    session = await client.new_session()
    turn = asyncio.create_task(client.ask(session, "run exactly: echo forbidden ."))
    await asyncio.wait_for(pending.wait(), timeout=1)
    await client.cancel(session)
    reply = await asyncio.wait_for(turn, timeout=1)
    assert reply.stop_reason == "cancelled"
    assert "forbidden" not in reply.text
    assert not client._permission_tasks


async def test_injected_stream_drop_fails_turn_without_automatic_replay(harness):
    pending = asyncio.Event()

    async def decide(request):
        pending.set()
        return request.option("allow_once").option_id

    client = harness.client
    client.set_permission_handler(decide)
    session = await client.new_session()
    prompt = "run exactly: sleep 30 ."
    turn = asyncio.create_task(client.ask(session, prompt))
    await asyncio.wait_for(pending.wait(), timeout=1)
    await harness.connections[0].close()
    with pytest.raises(TransportDisconnected):
        await asyncio.wait_for(turn, timeout=1)
    assert not client.connected
    assert len(harness.streams) == 1
    assert (await client.ask(session, "hello after drop")).text == "echo: hello after drop"
    assert len(harness.streams) == 2
    assert harness.sessions[session].history.count(("user", prompt)) == 1


async def test_injected_factory_transient_failure_uses_existing_backoff(harness):
    attempts = 0

    async def factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AgentUnavailable("computer temporarily offline")
        return await harness.factory()

    client = harness.client
    client._transport_factory = factory
    client._reconnect_delays = (0,)
    await client.ensure_connected()
    assert client.connected
    assert attempts == 2
    assert len(harness.streams) == 1


async def test_injected_stream_is_closed_when_sdk_construction_fails(harness, monkeypatch):
    def fail(*args):
        raise RuntimeError("cannot construct SDK connection")

    monkeypatch.setattr("acp_gateway.agents.client.connect_to_agent", fail)
    with pytest.raises(RuntimeError, match="cannot construct"):
        await harness.client.connect()
    assert harness.streams[0].closed.is_set()
    assert not harness.client.connected


async def test_injected_stream_is_closed_on_invalid_initialize(harness, monkeypatch):
    async def invalid_initialize(self, **kwargs):
        return {"protocolVersion": "invalid"}

    monkeypatch.setattr(FakeGooseAgent, "initialize", invalid_initialize)
    with pytest.raises(AgentUnavailable, match="invalid initialize response"):
        await harness.client.connect()
    assert harness.streams[0].closed.is_set()
    assert not harness.client.connected


async def test_cancelling_injected_handshake_closes_stream(harness, monkeypatch):
    initialized = asyncio.Event()

    async def blocked_initialize(self, **kwargs):
        initialized.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(FakeGooseAgent, "initialize", blocked_initialize)
    task = asyncio.create_task(harness.client.connect())
    await asyncio.wait_for(initialized.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.streams[0].closed.is_set()
    assert not harness.client.connected
