"""A dropped computer connection parks agent streams and resumes them losslessly."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import acp_gateway.connectors.relay as relay_module
from acp_gateway.agents import AgentClient, AgentUnavailable
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.policy import LocalAgentPolicy, PolicyTransport
from acp_gateway.connectors.protocol import AgentManifest, Hello
from acp_gateway.connectors.relay import (
    ACK_EVERY,
    RESUME_WINDOW,
    RelayPeer,
    StreamParking,
)
from acp_gateway.storage import Store
from fakes.fake_goose import FakeGooseServer

SECRET = "mock-local-" + "agent-credential"


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class LocalAgent:
    """In-memory local agent transport: the test plays the agent."""

    def __init__(self):
        self.closed = asyncio.Event()
        self.outgoing = asyncio.Queue()
        self.received = []

    async def send(self, message):
        self.received.append(message)

    async def receive(self):
        get = asyncio.create_task(self.outgoing.get())
        closed = asyncio.create_task(self.closed.wait())
        done, pending = await asyncio.wait({get, closed}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        return get.result() if get in done else None

    async def close(self):
        self.closed.set()


def note(index):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"n": index}}


@pytest.fixture
async def computer(tmp_path):
    store = Store.open_in(tmp_path / "data")
    token = tmp_path / "computer.key"
    store.computers.issue("work", token, display_name="Work")
    dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5)
    agents, peers, connections, tasks = [], [], [], []
    parking = StreamParking()
    local = SimpleNamespace(factory=None)

    async def factory(alias):
        assert alias == "echo"
        if local.factory is not None:
            return await local.factory()
        agent = LocalAgent()
        agents.append(agent)
        return agent

    async with dispatcher.listen("127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"

        async def connect(*, resume=True):
            connection, welcome = await connect_control(
                url,
                credential=token.read_text().strip(),
                hello=Hello(
                    computer_id="work",
                    agents=[AgentManifest(alias="echo", display_name="Echo")],
                ),
                stream_resume=resume,
            )
            negotiated = welcome.stream_resume == 1
            if negotiated:
                parking.shorten(RESUME_WINDOW)
            peer = RelayPeer(
                connection,
                welcome.connection_id,
                ["echo"],
                local_factory=factory,
                stream_resume=negotiated,
                parking=parking if negotiated else None,
                on_park=parking.park if negotiated else None,
            )
            tasks.append(asyncio.create_task(peer.run()))
            peers.append(peer)
            connections.append(connection)
            await peer.ready.wait()
            return peer

        def drop():
            # Abrupt network loss: no close handshake on either side.
            connections[-1].transport.abort()

        async def lose_locally():
            # Only the computer noticed the loss; the dispatcher still sees a live link.
            tasks[-1].cancel()
            await asyncio.gather(tasks[-1], return_exceptions=True)

        await connect()
        try:
            yield SimpleNamespace(
                dispatcher=dispatcher,
                registry=store.computers,
                agents=agents,
                peers=peers,
                parking=parking,
                connect=connect,
                drop=drop,
                lose_locally=lose_locally,
                local=local,
                tmp_path=tmp_path,
            )
        finally:
            parking.close_all()
            for parked in dispatcher.parked.values():
                parked.close_all()
            for connection in connections:
                await connection.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    store.close()


async def receive_all(stream, count):
    return [await asyncio.wait_for(stream.receive(), 2) for _ in range(count)]


async def test_dropped_connection_resumes_stream_without_loss_or_duplicates(computer):
    stream = await computer.dispatcher.open_agent("work", "echo")
    agent = computer.agents[0]
    total = ACK_EVERY * 3
    for index in range(total):
        agent.outgoing.put_nowait(note(index))
    assert [m["params"]["n"] for m in await receive_all(stream, total)] == list(range(total))
    old = computer.peers[0]
    await eventually(lambda: old.streams and next(iter(old.streams.values())).sent == total)

    computer.drop()
    await eventually(lambda: len(computer.parking) == 1)
    await eventually(lambda: "work" not in computer.dispatcher.active)
    assert not stream.closed.is_set() and not agent.closed.is_set()

    # Both directions keep working while the computer is offline.
    for index in range(total, total + 5):
        agent.outgoing.put_nowait(note(index))
    await stream.send({"jsonrpc": "2.0", "id": 7, "method": "session/cancel", "params": {}})

    new = await computer.connect()
    await eventually(lambda: stream.peer is not None and stream.peer.epoch != old.epoch)
    assert len(computer.parking) == 0 and new.streams
    replayed = await receive_all(stream, 5)
    assert [m["params"]["n"] for m in replayed] == list(range(total, total + 5))
    await eventually(lambda: len(agent.received) == 1)
    assert agent.received[0]["id"] == 7
    assert computer.agents == [agent]  # the local agent was never restarted

    agent.outgoing.put_nowait(note(999))
    assert (await asyncio.wait_for(stream.receive(), 2))["params"]["n"] == 999
    await stream.close()
    await eventually(agent.closed.is_set)


async def test_replaced_connection_resumes_stream(computer):
    stream = await computer.dispatcher.open_agent("work", "echo")
    agent = computer.agents[0]
    # The computer reconnects before the dispatcher noticed the old link died.
    await computer.lose_locally()
    assert len(computer.parking) == 1 and "work" in computer.dispatcher.active
    await computer.connect()
    agent.outgoing.put_nowait(note(1))
    assert (await asyncio.wait_for(stream.receive(), 2))["params"]["n"] == 1
    assert not stream.closed.is_set() and computer.agents == [agent]


async def test_parked_stream_expires_and_closes_local_agent(computer):
    stream = await computer.dispatcher.open_agent("work", "echo")
    agent = computer.agents[0]
    computer.parking.grace = 0.2
    computer.drop()
    await eventually(agent.closed.is_set)
    await eventually(lambda: "work" in computer.dispatcher.parked)
    computer.dispatcher.parked["work"].shorten(0)
    await eventually(stream.closed.is_set)
    assert await stream.receive() is None


async def test_reconnect_without_reclaim_abandons_parked_work(computer, monkeypatch):
    stream = await computer.dispatcher.open_agent("work", "echo")
    agent = computer.agents[0]
    computer.drop()
    await eventually(lambda: len(computer.parking) == 1)
    await eventually(lambda: "work" not in computer.dispatcher.active)
    # The dispatcher lost its state (restart): nothing reclaims the stream.
    computer.dispatcher.parked.pop("work").close_all()
    assert stream.closed.is_set()
    monkeypatch.setattr(relay_module, "RESUME_WINDOW", 0.1)
    computer.parking.shorten(0.1)
    await computer.connect()
    await eventually(agent.closed.is_set)


async def test_restarted_computer_fails_parked_dispatcher_stream_quickly(computer):
    stream = await computer.dispatcher.open_agent("work", "echo")
    computer.drop()
    await eventually(lambda: "work" in computer.dispatcher.parked)
    await eventually(lambda: len(computer.parking) == 1)
    computer.parking.close_all()  # the connector process restarted
    await computer.connect()
    await eventually(stream.closed.is_set, timeout=2)
    assert await stream.receive() is None
    assert not len(computer.dispatcher.parked["work"])


async def test_revocation_never_parks_streams(computer):
    stream = await computer.dispatcher.open_agent("work", "echo")
    agent = computer.agents[0]
    computer.registry.revoke("work")
    assert stream.closed.is_set()
    assert "work" not in computer.dispatcher.parked
    await eventually(agent.closed.is_set)
    with pytest.raises(AgentUnavailable):
        await computer.dispatcher.open_agent("work", "echo")


async def test_revocation_while_offline_drops_parked_streams(computer):
    stream = await computer.dispatcher.open_agent("work", "echo")
    computer.drop()
    await eventually(lambda: "work" in computer.dispatcher.parked)
    assert not stream.closed.is_set()
    computer.registry.revoke("work")
    assert stream.closed.is_set()
    assert "work" not in computer.dispatcher.parked


async def test_legacy_connector_keeps_close_on_drop(computer):
    stream = await computer.dispatcher.open_agent("work", "echo")
    agent = computer.agents[0]
    await computer.connect(resume=False)
    await eventually(stream.closed.is_set)
    await eventually(agent.closed.is_set)
    assert "work" not in computer.dispatcher.parked or not len(computer.dispatcher.parked["work"])
    with contextlib.suppress(Exception):
        await stream.close()


async def test_agent_turn_survives_connection_drop_without_replay(computer):
    with FakeGooseServer(SECRET) as goose:
        profile = AgentProfile(alias="echo", kind="goose", url=goose.url("ws"), default_cwd="/work")
        direct = AgentClient(profile, SecretStr(SECRET), pin_dir=computer.tmp_path / "local")

        async def open_goose():
            transport, _ = await direct.open_transport()
            return PolicyTransport(transport, LocalAgentPolicy.from_profile(profile))

        computer.local.factory = open_goose
        client = AgentClient(
            profile.model_copy(update={"backend": "connector", "computer_id": "work", "url": None}),
            pin_dir=computer.tmp_path / "remote",
            transport_factory=lambda: computer.dispatcher.open_agent("work", "echo"),
            reconnect_delays=(),
            request_timeout=5,
        )
        asked = asyncio.Event()
        decision = asyncio.get_running_loop().create_future()

        async def decide(request):
            asked.set()
            await decision
            return request.option("allow_once").option_id

        client.set_permission_handler(decide)
        try:
            session = await client.new_session()
            prompt = "run exactly: echo survived ."
            turn = asyncio.create_task(client.ask(session, prompt))
            await asyncio.wait_for(asked.wait(), 3)

            computer.drop()
            await eventually(lambda: "work" in computer.dispatcher.parked)
            # The approval is given while the computer is offline.
            decision.set_result(None)
            await asyncio.sleep(0.1)
            assert not turn.done() and client.connected

            await computer.connect()
            reply = await asyncio.wait_for(turn, 5)
            assert reply.text == "survived"
            assert goose.log.count("load_session") == 0
            assert goose.sessions[session].history.count(("user", prompt)) == 1
        finally:
            await client.close()
