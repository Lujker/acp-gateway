"""VPS ACP client -> real WS relay -> local Hermes adapter -> host HTTP/WS mock."""

import asyncio
from types import SimpleNamespace

import pytest

from acp_gateway.agents import AgentClient, AgentError, SessionNotFound, TransportDisconnected
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.policy import LocalAgentPolicy, PolicyTransport
from acp_gateway.connectors.protocol import AgentManifest, Hello
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.storage import Store
from fakes.fake_hermes import FakeHermes


@pytest.fixture
async def relay(tmp_path):
    async with FakeHermes(tmp_path / "locks") as backend:
        profile = AgentProfile(
            alias="hermes",
            kind="hermes",
            backend="hermes",
            default_cwd=r"C:\work",
            hermes={"lock_dir": backend.directory},
        )
        local = AgentClient(profile, pin_dir=tmp_path / "local-pins")
        store = Store.open_in(tmp_path / "store")
        token = tmp_path / "computer.key"
        store.computers.issue("home", token, display_name="Home")
        dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5)
        peers, connections, tasks = [], [], []

        async def factory(alias):
            assert alias == "hermes"
            transport, _ = await local.open_transport()
            return PolicyTransport(transport, LocalAgentPolicy.from_profile(profile))

        async with dispatcher.listen("127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"

            async def computer():
                connection, welcome = await connect_control(
                    url,
                    credential=token.read_text().strip(),
                    hello=Hello(
                        computer_id="home",
                        agents=[AgentManifest(alias="hermes", display_name="Hermes")],
                    ),
                )
                peer = RelayPeer(
                    connection, welcome.connection_id, ["hermes"], local_factory=factory
                )
                connections.append(connection)
                peers.append(peer)
                tasks.append(asyncio.create_task(peer.run()))
                await peer.ready.wait()
                return peer

            remote = AgentClient(
                AgentProfile(
                    alias="hermes",
                    kind="hermes",
                    backend="connector",
                    computer_id="home",
                    default_cwd=r"C:\work",
                ),
                pin_dir=tmp_path / "remote-pins",
                transport_factory=lambda: dispatcher.open_agent("home", "hermes"),
                reconnect_delays=(),
            )
            await computer()
            try:
                yield SimpleNamespace(
                    backend=backend, client=remote, computer=computer, peers=peers
                )
            finally:
                await remote.close()
                for connection in connections:
                    await connection.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


@pytest.mark.parametrize("approve", [True, False])
async def test_hermes_relay_streams_and_once_only_permissions(relay, approve):
    decisions = []

    async def decide(request):
        decisions.append(request)
        return request.option("allow_once" if approve else "reject_once").option_id

    client = relay.client
    client.set_permission_handler(decide)
    session = await client.new_session()
    assert client.session_binding(session)["stored_id"] == "stored-live-1"
    assert not client.supports_cancel and not client.agent_capabilities.load_session
    assert (await client.ask(session, "chunks")).text == "hello world"
    assert (await client.ask(session, "permit")).text == ("allowed" if approve else "denied")
    assert len(decisions) == 1
    with pytest.raises(AgentError, match="cancellation is unavailable"):
        await client.cancel(session)
    assert all(
        f["method"] in {"client.capabilities", "session.create", "prompt.submit"}
        for f in relay.backend.calls
    )


async def test_hermes_relay_epoch_change_does_not_replay_or_adopt(relay):
    client = relay.client
    session = await client.new_session()
    turn = asyncio.create_task(client.ask(session, "hold"))
    await asyncio.wait_for(relay.backend.started.wait(), 2)
    original = relay.peers[0].epoch
    assert (await relay.computer()).epoch != original
    with pytest.raises(TransportDisconnected):
        await asyncio.wait_for(turn, 2)
    with pytest.raises(SessionNotFound):
        await client.ask(session, "retry")
    assert len([f for f in relay.backend.calls if f["method"] == "prompt.submit"]) == 1
    # A deliberate new conversation is allowed on the replacement connection.
    fresh = await client.new_session()
    assert fresh != session
    assert (await client.ask(fresh, "fresh conversation")).text == "fresh conversation"


@pytest.mark.parametrize("deferred", [False, True])
async def test_model_control_crosses_real_relay_and_preserves_dialogue(relay, deferred):
    client = relay.client
    relay.backend.defer_models = deferred
    session = await client.new_session()
    binding = client.session_binding(session)
    assert client.supports_model_control
    assert (await client.model_options(session))["model"] == "model-a"
    assert (await client.set_model(session, provider="provider-b", model="model-b"))["status"] == (
        "deferred" if deferred else "accepted"
    )
    if deferred:
        assert (await client.ask(session, "apply pending")).text == "apply pending"
    result = await client.model_options(session)
    assert (result["provider"], result["model"], result["pending"]) == (
        "provider-b",
        "model-b",
        None,
    )
    assert client.session_binding(session) == binding
    assert len(relay.backend.sessions) == 1
