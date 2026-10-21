"""Live discovery without server profiles, routing, replacement and persisted offline history."""

import asyncio
import contextlib

import httpx
import pytest
from test_connector_relay import eventually

from acp_gateway.agents import AgentUnavailable
from acp_gateway.config import AppConfig, SecretStore, Settings
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.protocol import AgentManifest, Hello
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.daemon import configured_app
from acp_gateway.storage import Conversation

pytest_plugins = ["test_connector_relay"]


async def test_catalog_without_server_profiles_survives_restart(relay, tmp_path, monkeypatch):
    listeners = []
    original = ControlDispatcher.listen

    @contextlib.asynccontextmanager
    async def listen(self, host, port, *, tls=None):
        async with original(self, host, 0, tls=tls) as server:
            listeners.append(server)
            yield server

    monkeypatch.setattr(ControlDispatcher, "listen", listen)
    config = AppConfig(
        Settings(data_dir=tmp_path / "runtime", connector={"enabled": True}),
        SecretStore({"ACPGW_API_TOKEN": "mock-owner", "ACPGW_MCP_TOKEN": "mock-mcp"}),
        None,
        None,
    )
    key = tmp_path / "runtime.key"
    with configured_app(config) as app:
        registry = app.state.core.store.computers
        registry.issue("work", key, display_name="Work")
        async with app.router.lifespan_context(app):
            assert not app.state.core.agents
            mcp = app.state.core.channels["mcp"].server
            assert not await mcp.list_tools()
            url = f"ws://127.0.0.1:{listeners[0].sockets[0].getsockname()[1]}/connect"

            async def register(manifests):
                connection, _welcome = await connect_control(
                    url,
                    credential=key.read_text().strip(),
                    hello=Hello(version=2, computer_id="work", agents=manifests),
                )
                peer = RelayPeer(
                    connection,
                    _welcome.connection_id,
                    [a.alias for a in manifests],
                    local_factory=relay.local_factory,
                )
                task = asyncio.create_task(peer.run())
                await asyncio.wait_for(peer.ready.wait(), 3)
                return connection, task

            manifest = AgentManifest(
                alias="goose",
                display_name="Local Goose",
                kind="goose",
                default_cwd="/work",
                session_mode="smart_approve",
            )
            connection, runner = await register([manifest])
            try:
                assert app.state.core.agent("work/goose").profile.default_cwd == "/work"
                assert len(await mcp.list_tools()) == 6
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer mock-owner"},
                ) as api:
                    health = (await api.get("/health")).json()
                    assert health["agents"][0]["alias"] == "work/goose"
                    session = await api.post("/sessions", json={"agent": "work/goose"})
                    assert session.status_code == 201, session.text
                    assert session.json()["cwd"] == "/work"
                    reply = await api.post(
                        "/messages", json={"agent": "work/goose", "text": "say pong", "wait": 2}
                    )
                    assert reply.json()["answer"] == "pong"
                    assert (
                        await mcp.call_tool("work__goose_ask", {"text": "say pong", "wait": 2})
                    )[1]["answer"] == "pong"
                second_key = tmp_path / "home.key"
                registry.issue("home", second_key, display_name="Home")
                second_connection, second_welcome = await connect_control(
                    url,
                    credential=second_key.read_text().strip(),
                    hello=Hello(version=2, computer_id="home", agents=[manifest]),
                )
                second_peer = RelayPeer(
                    second_connection,
                    second_welcome.connection_id,
                    ["goose"],
                    local_factory=relay.local_factory,
                )
                second_runner = asyncio.create_task(second_peer.run())
                try:
                    await asyncio.wait_for(second_peer.ready.wait(), 3)
                    assert set(app.state.core.agents) == {"work/goose", "home/goose"}
                    assert len(await mcp.list_tools()) == 12
                    other = await app.state.core.new_session(
                        Conversation("cli", "default", "home/goose")
                    )
                    assert other.cwd == "/work"
                    registry.revoke("home")
                finally:
                    await second_connection.close()
                    await asyncio.gather(second_runner, return_exceptions=True)
                await connection.close()
                await runner
                await eventually(lambda: not app.state.dispatcher.active)
                assert "work/goose" in app.state.core.agents  # ordinary offline retains catalog
            finally:
                await connection.close()
                await asyncio.gather(runner, return_exceptions=True)
    with configured_app(config) as restored:
        core = restored.state.core
        assert "work/goose" in core.agents
        assert not core.agent("work/goose").connected
        assert core.store.schema_version == 10
        assert core.store.sessions(Conversation("cli", "default", "work/goose"))
        assert len(await core.channels["mcp"].server.list_tools()) == 6
        async with restored.router.lifespan_context(restored):
            # The same computer removes goose and announces a different alias on reconnect.
            url = f"ws://127.0.0.1:{listeners[-1].sockets[0].getsockname()[1]}/connect"
            connection, _welcome = await connect_control(
                url,
                credential=key.read_text().strip(),
                hello=Hello(
                    version=2,
                    computer_id="work",
                    agents=[
                        AgentManifest(
                            alias="other",
                            display_name="Other",
                            kind="generic",
                            default_cwd="/other",
                        )
                    ],
                ),
            )
            try:
                assert set(core.agents) == {"work/other"}
                tools = await core.channels["mcp"].server.list_tools()
                assert all(t.name.startswith("work__other_") for t in tools)
                registry = core.store.computers
                registry.revoke("work")
                with pytest.raises(AgentUnavailable, match=r"offline|access"):
                    await restored.state.dispatcher.open_agent("work", "other")
            finally:
                await connection.close()
    with configured_app(config) as revoked:
        assert not revoked.state.core.agents
