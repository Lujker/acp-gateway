"""Real WS ingress -> multiplexed relay -> guarded local WS -> ACP SDK."""

import asyncio
import contextlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from acp_gateway.agents import (
    AgentClient,
    AgentUnavailable,
    AuthenticationFailed,
    TransportDisconnected,
)
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.policy import LocalAgentPolicy, PolicyTransport
from acp_gateway.connectors.protocol import AgentManifest, Data, Hello, encode_frame
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.daemon import configured_app
from acp_gateway.storage import Store
from fakes.fake_goose import FakeGooseServer

SECRET = "mock-local-" + "agent-credential"


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def relay(tmp_path):
    with FakeGooseServer(SECRET) as goose:
        store = Store.open_in(tmp_path / "data")
        token = tmp_path / "computer.key"
        store.computers.issue("work", token, display_name="Work")
        dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5)
        profile = AgentProfile(
            alias="goose", kind="goose", url=goose.url("ws"), default_cwd="/work"
        )
        local = AgentClient(profile, SecretStr(SECRET), pin_dir=tmp_path / "local-pins")
        local_streams = []

        async def factory(alias):
            assert alias == "goose"
            transport, _ = await local.open_transport()
            guarded = PolicyTransport(transport, LocalAgentPolicy.from_profile(profile))
            local_streams.append(guarded)
            return guarded

        remote_profile = AgentProfile(
            alias="goose",
            kind="goose",
            backend="connector",
            computer_id="work",
            default_cwd="/work",
        )
        clients, peers, connections, tasks = [], [], [], []
        async with dispatcher.listen("127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"

            async def computer():
                connection, welcome = await connect_control(
                    url,
                    credential=token.read_text().strip(),
                    hello=Hello(
                        computer_id="work",
                        agents=[AgentManifest(alias="goose", display_name="Goose")],
                    ),
                )
                peer = RelayPeer(
                    connection, welcome.connection_id, ["goose"], local_factory=factory
                )
                task = asyncio.create_task(peer.run())
                peers.append(peer)
                connections.append(connection)
                tasks.append(task)
                await peer.ready.wait()
                return peer

            def client():
                result = AgentClient(
                    remote_profile,
                    pin_dir=tmp_path / "unused-pins",
                    transport_factory=lambda: dispatcher.open_agent("work", "goose"),
                    reconnect_delays=(),
                    request_timeout=2,
                )
                clients.append(result)
                return result

            state = SimpleNamespace(
                dispatcher=dispatcher,
                registry=store.computers,
                goose=goose,
                client=client(),
                new_client=client,
                computer=computer,
                peers=peers,
                connections=connections,
                local_streams=local_streams,
                local_factory=factory,
            )
            await computer()
            try:
                yield state
            finally:
                for item in clients:
                    await item.close()
                for connection in connections:
                    await connection.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


@pytest.mark.parametrize("approve", [False, True])
async def test_relay_sessions_modes_streamed_reply_and_permissions(relay, approve):
    decisions = []

    async def decide(request):
        decisions.append(request)
        assert {o.kind for o in request.options} == {"allow_once", "reject_once"}
        return request.option("allow_once" if approve else "reject_once").option_id

    client = relay.client
    client.set_permission_handler(decide)
    session = await client.new_session()
    assert client.connected
    assert client.tls_pin is None
    assert (await client.ask(session, "code word remote")).text == "OK"
    assert (await client.ask(session, "What was the code word")).text == "remote"
    reply = await client.ask(session, "run exactly: echo permitted .")
    assert reply.text == ("permitted" if approve else "DENIED")
    assert len(decisions) == 1
    assert relay.local_streams[0].policy.session_mode == "smart_approve"


async def test_relay_cancel_pending_permission(relay):
    pending = asyncio.Event()

    async def decide(request):
        pending.set()
        await asyncio.Event().wait()

    client = relay.client
    client.set_permission_handler(decide)
    session = await client.new_session()
    turn = asyncio.create_task(client.ask(session, "run exactly: echo forbidden ."))
    await asyncio.wait_for(pending.wait(), 2)
    await client.cancel(session)
    assert (await asyncio.wait_for(turn, 2)).stop_reason == "cancelled"
    assert not client._permission_tasks


async def test_epoch_replacement_fails_pending_prompt_and_does_not_replay(relay):
    pending = asyncio.Event()

    async def decide(request):
        pending.set()
        await asyncio.Event().wait()

    client = relay.client
    client.set_permission_handler(decide)
    session = await client.new_session()
    prompt = "run exactly: echo do-not-replay ."
    turn = asyncio.create_task(client.ask(session, prompt))
    await asyncio.wait_for(pending.wait(), 2)
    old_epoch = relay.peers[0].epoch
    new_peer = await relay.computer()
    assert new_peer.epoch != old_epoch
    with pytest.raises(TransportDisconnected):
        await asyncio.wait_for(turn, 2)
    assert not client.connected
    assert (
        await client.ask(session, "hello after reconnect")
    ).text == "echo: hello after reconnect"
    assert relay.goose.log.count("load_session") == 1
    assert relay.goose.sessions[session].history.count(("user", prompt)) == 1


async def test_closing_one_stream_preserves_other_agent_client(relay):
    first = relay.client
    second = relay.new_client()
    one = await first.new_session()
    two = await second.new_session()
    await first.close()
    assert (await second.ask(two, "say pong")).text == "pong"
    assert not first.connected and second.connected
    assert (await first.ask(one, "say pong")).text == "pong"


@pytest.mark.parametrize("payload_size,count", [(800_000, 3), (1, 130)])
async def test_slow_stream_overflow_does_not_disconnect_other_stream(relay, payload_size, count):
    session = await relay.client.new_session()
    stalled = await relay.dispatcher.open_agent("work", "goose")
    try:
        for _ in range(count):
            await relay.peers[0].send(
                Data(
                    **stalled.route,
                    message={
                        "jsonrpc": "2.0",
                        "method": "notice",
                        "params": {"text": "x" * payload_size},
                    },
                )
            )
        await eventually(stalled.closed.is_set)
        assert (await stalled.receive()) is None
        assert not relay.peers[0].closed.is_set()
        assert (await relay.client.ask(session, "say pong")).text == "pong"
    finally:
        await stalled.close()


async def test_oversized_outgoing_message_fails_only_its_stream(relay):
    session = await relay.client.new_session()
    stream = await relay.dispatcher.open_agent("work", "goose")
    with pytest.raises(ConnectionError, match="exceeds limits"):
        await stream.send(
            {"jsonrpc": "2.0", "method": "notice", "params": {"text": "x" * 1_048_576}}
        )
    assert stream.closed.is_set()
    assert (await relay.client.ask(session, "say pong")).text == "pong"


async def test_cancelled_local_open_does_not_block_another_stream(relay):
    session = await relay.client.new_session()
    peer = relay.peers[0]
    original = peer.local_factory
    opening = asyncio.Event()
    released = asyncio.Event()

    async def stalled_factory(alias):
        opening.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    peer.local_factory = stalled_factory
    task = asyncio.create_task(relay.dispatcher.open_agent("work", "goose"))
    await asyncio.wait_for(opening.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(released.wait(), 2)
    peer.local_factory = original
    assert (await relay.client.ask(session, "say pong")).text == "pong"


async def test_local_policy_violation_closes_only_affected_stream(relay):
    first, second = relay.client, relay.new_client()
    session = await second.new_session()
    with pytest.raises((AgentUnavailable, TransportDisconnected)):
        await first.new_session("/not-allowed")
    assert (await second.ask(session, "say pong")).text == "pong"
    assert not relay.peers[0].closed.is_set()


async def test_in_process_revoke_fences_streams_immediately(relay):
    await relay.client.new_session()
    registration = relay.dispatcher.active["work"]
    stream = relay.client._transport
    relay.registry.revoke("work")
    assert registration.relay.closed.is_set()
    assert stream.closed.is_set()
    with pytest.raises(AgentUnavailable):
        await relay.dispatcher.open_agent("work", "goose")
    await eventually(lambda: not relay.dispatcher.active)


async def test_offline_agent_and_unadvertised_alias_never_open_local_socket(relay):
    with pytest.raises(AgentUnavailable):
        await relay.dispatcher.open_agent("other", "goose")
    with pytest.raises(AgentUnavailable):
        await relay.dispatcher.open_agent("work", "unknown")
    assert not relay.local_streams


async def test_local_authentication_failure_is_fatal_without_retries(relay):
    attempts = []

    async def rejected(alias):
        attempts.append(alias)
        raise AuthenticationFailed("local agent refused access")

    relay.peers[0].local_factory = rejected
    relay.client._reconnect_delays = (0, 0)
    with pytest.raises(AuthenticationFailed):
        await relay.client.ensure_connected()
    assert attempts == ["goose"]
    assert not relay.peers[0].closed.is_set()


async def test_old_epoch_data_cannot_reach_live_stream(relay):
    await relay.client.new_session()
    transport = relay.client._transport
    forged = Data(
        epoch=uuid4(),
        stream=transport.stream,
        alias="goose",
        message={"jsonrpc": "2.0", "method": "session/update", "params": {}},
    )
    await relay.connections[0].send(encode_frame(forged))
    await eventually(lambda: not relay.dispatcher.active)
    assert transport.closed.is_set()
    with contextlib.suppress(Exception):
        await relay.connections[0].wait_closed()


async def test_serve_runtime_owner_api_sessions_permissions_and_revocation(
    relay, tmp_path, monkeypatch
):
    # The actual configured_app lifespan owns ingress and core in the same loop.
    listeners = []
    original = ControlDispatcher.listen

    @contextlib.asynccontextmanager
    async def listen(self, host, port, *, tls=None):
        async with original(self, host, 0, tls=tls) as server:
            listeners.append(server)
            yield server

    monkeypatch.setattr(ControlDispatcher, "listen", listen)
    settings = Settings(
        data_dir=tmp_path / "runtime",
        connector={"enabled": True},
        agents=[
            dict(
                alias="goose",
                kind="goose",
                backend="connector",
                computer_id="work",
                default_cwd="/work",
            )
        ],
    )
    owner = "mock-runtime-owner-credential"
    config = AppConfig(
        settings,
        SecretStore({"ACPGW_API_TOKEN": owner, "ACPGW_MCP_TOKEN": "mock-runtime-mcp-credential"}),
        None,
        None,
    )
    with configured_app(config) as app:
        token = tmp_path / "runtime.key"
        registry = app.state.core.store.computers
        registry.issue("work", token, display_name="Work")
        async with app.router.lifespan_context(app):
            mcp = app.state.core.channels["mcp"].server
            assert {tool.name for tool in await mcp.list_tools()} == {
                "work__goose_" + suffix
                for suffix in ("status", "sessions", "new_session", "ask", "result", "cancel")
            }
            port = listeners[0].sockets[0].getsockname()[1]
            connection, welcome = await connect_control(
                f"ws://127.0.0.1:{port}/connect",
                credential=token.read_text().strip(),
                hello=Hello(
                    computer_id="work", agents=[AgentManifest(alias="goose", display_name="Goose")]
                ),
            )
            peer = RelayPeer(
                connection, welcome.connection_id, ["goose"], local_factory=relay.local_factory
            )
            runner = asyncio.create_task(peer.run())
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="http://127.0.0.1",
                    headers={"Authorization": f"Bearer {owner}"},
                ) as api:
                    health = (await api.get("/health")).json()
                    assert health["computers"][0]["computer_id"] == "work"
                    assert health["agents"][0]["alias"] == "work/goose"
                    assert not health["agents"][0]["connected"]
                    session = await api.post("/sessions", json={"agent": "work/goose"})
                    assert session.status_code == 201, session.text
                    probe = await api.get("/health/agents/work/goose")
                    assert probe.status_code == 200 and probe.json()["connected"]
                    reply = await api.post(
                        "/messages", json={"agent": "work/goose", "text": "say pong", "wait": 2}
                    )
                    assert reply.json()["answer"] == "pong"
                    assert reply.json()["status"] == "completed"
                    mcp_reply = await mcp.call_tool(
                        "work__goose_ask", {"text": "say pong", "wait": 2}
                    )
                    assert mcp_reply[1]["answer"] == "pong"
                    lease = app.state.cli.attach("test-human")
                    job = (
                        await api.post(
                            "/messages",
                            json={
                                "agent": "work/goose",
                                "text": "run exactly: echo human-approved .",
                            },
                        )
                    ).json()
                    await eventually(lambda: bool(app.state.core.pending_approvals("cli")))
                    approval = (await api.get("/approvals")).json()["approvals"][0]
                    option = next(
                        o for o in approval["request"]["options"] if o["kind"] == "allow_once"
                    )
                    decision = await api.post(
                        f"/approvals/{approval['id']}",
                        json={"option_id": option["option_id"], "lease_id": lease},
                    )
                    assert decision.status_code == 200, decision.text
                    final = await api.get(f"/jobs/{job['id']}", params={"wait": 2})
                    assert final.json()["answer"] == "human-approved"
                    app.state.cli.detach(lease)
                    registry.revoke("work")
                    assert not app.state.core.agent("work/goose").connected
                    assert (await api.get("/health/agents/work/goose")).status_code == 503
                    assert not (await api.get("/health")).json()["computers"][0]["connected"]
            finally:
                await connection.close()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        assert not app.state.dispatcher.active
