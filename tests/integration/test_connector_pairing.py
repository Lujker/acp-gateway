import json
import secrets

import pytest
from websockets.asyncio.client import connect

from acp_gateway.connectors.control import ControlDispatcher
from acp_gateway.storage.db import Store


@pytest.mark.asyncio
async def test_bounded_pairing_on_existing_listener_and_normal_auth_is_separate(tmp_path):
    from acp_gateway.pairing import PairingService

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    owner = pairing.bootstrap([123])
    dispatcher = ControlDispatcher(store.computers, connect_path="/proxy/connect", pairing=pairing)
    async with dispatcher.listen("127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        async def rpc(payload):
            async with connect(f"ws://127.0.0.1:{port}/proxy/connect/pair", proxy=None) as ws:
                await ws.send(json.dumps(payload))
                return json.loads(await ws.recv())

        secret = secrets.token_urlsafe(32)
        request = await rpc(
            {
                "action": "request",
                "owner_id": owner["owner_id"],
                "device_code": secret,
                "name": "PC",
            }
        )
        unknown = await rpc(
            {
                "action": "request",
                "owner_id": "unknown",
                "device_code": secrets.token_urlsafe(32),
                "name": "PC",
            }
        )
        assert request.keys() == unknown.keys()
        assert request["state"] == unknown["state"] == "pending"
        assert len(pairing.pending("local")) == 1
        assert await rpc(
            {
                "action": "claim",
                "request_id": request["request_id"],
                "device_code": secrets.token_urlsafe(32),
            }
        ) == {"error": "pairing request rejected"}
        pairing.decide("telegram:123", request["request_id"], request["verification"], approve=True)
        claimed = await rpc(
            {"action": "claim", "request_id": request["request_id"], "device_code": secret}
        )
        assert claimed["status"] == "issued"
        assert not store.computers.authenticate(claimed["computer_id"], claimed["credential"])
        ack = await rpc(
            {
                "action": "ack",
                "request_id": request["request_id"],
                "device_code": secret,
                "credential": claimed["credential"],
            }
        )
        assert ack["status"] == "active"
        assert store.computers.authenticate(claimed["computer_id"], claimed["credential"])
        from websockets.exceptions import ConnectionClosedError

        with pytest.raises(ConnectionClosedError):
            await rpc(
                {
                    "action": "request",
                    "owner_id": owner["owner_id"],
                    "device_code": secret,
                    "name": "x" * 5000,
                }
            )
    store.close()


@pytest.mark.asyncio
async def test_real_pairing_client_then_catalog_new_dialogue_and_mock_agent_result(
    relay, tmp_path, monkeypatch
):
    import asyncio
    import contextlib

    import httpx

    from acp_gateway.config import AppConfig, SecretStore, Settings
    from acp_gateway.connectors.control import connect_control
    from acp_gateway.connectors.pairing_client import PairingClient
    from acp_gateway.connectors.protocol import AgentManifest, Hello
    from acp_gateway.connectors.relay import RelayPeer
    from acp_gateway.daemon import configured_app

    listeners = []
    original = ControlDispatcher.listen

    @contextlib.asynccontextmanager
    async def listen(self, host, port, *, tls=None):
        async with original(self, host, 0, tls=tls) as server:
            listeners.append(server)
            yield server

    monkeypatch.setattr(ControlDispatcher, "listen", listen)
    config = AppConfig(
        Settings(data_dir=tmp_path / "dispatcher", connector={"enabled": True}),
        SecretStore({"ACPGW_API_TOKEN": "mock-owner"}),
        None,
        None,
    )
    with configured_app(config) as app:
        pairing = app.state.dispatcher.pairing.service
        owner = pairing.bootstrap([])
        async with app.router.lifespan_context(app):
            port = listeners[0].sockets[0].getsockname()[1]
            client = PairingClient(tmp_path / "applicant/pending.json")
            req = await client.begin(
                f"ws://127.0.0.1:{port}/connect", owner["owner_id"], "Work", allow_ws=True
            )
            assert await client.finish() == {"status": "pending"}
            pairing.decide("local", req["request_id"], req["verification"], approve=True)
            saved = await client.finish()
            token = __import__("pathlib").Path(saved["token_file"]).read_text().strip()
            connection, welcome = await connect_control(
                saved["dispatcher_url"],
                credential=token,
                hello=Hello(
                    version=2,
                    computer_id=saved["computer_id"],
                    agents=[
                        AgentManifest(
                            alias="goose",
                            display_name="Mock Goose",
                            kind="goose",
                            default_cwd="/work",
                            session_mode="smart_approve",
                        )
                    ],
                ),
            )
            peer = RelayPeer(
                connection, welcome.connection_id, ["goose"], local_factory=relay.local_factory
            )
            task = asyncio.create_task(peer.run())
            try:
                await asyncio.wait_for(peer.ready.wait(), 3)
                route = saved["computer_id"] + "/goose"
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer mock-owner"},
                ) as api:
                    session = await api.post("/sessions", json={"agent": route})
                    assert session.status_code == 201, session.text
                    response = await api.post(
                        "/messages", json={"agent": route, "text": "say pong", "wait": 2}
                    )
                    assert response.json()["answer"] == "pong"
            finally:
                await connection.close()
                await asyncio.gather(task, return_exceptions=True)


pytest_plugins = ["test_connector_relay"]


@pytest.mark.asyncio
async def test_legacy_rebind_signal_and_live_generation_fence(tmp_path):
    import asyncio

    from acp_gateway.connectors.control import ConnectorAccessError, connect_control
    from acp_gateway.connectors.protocol import AgentManifest, Hello
    from acp_gateway.pairing import PairingService

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    owner = pairing.bootstrap([])
    store.computers.issue("old", tmp_path / "old.key", display_name="Old")
    token = (tmp_path / "old.key").read_text().strip()
    dispatcher = ControlDispatcher(store.computers, pairing=pairing)
    async with dispatcher.listen("127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"
        connection, _welcome = await connect_control(
            url,
            credential=token,
            hello=Hello(
                version=2,
                computer_id="old",
                agents=[
                    AgentManifest(
                        alias="mock",
                        display_name="Mock",
                        kind="goose",
                        default_cwd="/work",
                        session_mode="smart_approve",
                    )
                ],
            ),
        )
        pairing.activate_migration("local", confirm=True)
        await asyncio.wait_for(connection.wait_closed(), 3)
        assert connection.close_code == 1008
        with pytest.raises(ConnectorAccessError, match="migration-required"):
            await connect_control(
                url,
                credential=token,
                hello=Hello(
                    version=2,
                    computer_id="old",
                    agents=[
                        AgentManifest(
                            alias="mock",
                            display_name="Mock",
                            kind="goose",
                            default_cwd="/work",
                            session_mode="smart_approve",
                        )
                    ],
                ),
            )
        secret = secrets.token_urlsafe(32)
        req = pairing.request(owner["owner_id"], secret, "Old", target="old")
        pairing.decide("local", req["request_id"], req["verification"], approve=True)
        issued = pairing.claim(req["request_id"], secret)
        pairing.ack(req["request_id"], secret, issued["credential"])
        assert issued["computer_id"] == "old"
        assert not store.computers.authenticate("old", token)
        assert store.computers.authenticate("old", issued["credential"])
        assert pairing.activate_migration("local", confirm=True)["status"] == "already_active"
        assert store.computers.authenticate("old", issued["credential"])
    store.close()


@pytest.mark.asyncio
async def test_wss_uses_normal_ca_and_hostname_validation_no_pin_or_downgrade(
    tmp_path, monkeypatch
):
    import ssl

    from tests.fakes.certs import make_cert

    from acp_gateway.connectors.pairing_client import PairingClient
    from acp_gateway.pairing import PairingService

    cert, key = make_cert(tmp_path, ca=True, hostname="localhost")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    store = Store.open_in(tmp_path / "server")
    pairing = PairingService(store, boundary=tmp_path / "server/pairing-boundary.json")
    owner = pairing.bootstrap([])
    dispatcher = ControlDispatcher(store.computers, pairing=pairing)
    async with dispatcher.listen("127.0.0.1", 0, tls=tls) as server:
        port = server.sockets[0].getsockname()[1]
        client = PairingClient(tmp_path / "applicant/pending.json")
        with pytest.raises(OSError, match="no downgrade"):
            await client.begin(f"wss://localhost:{port}/connect", owner["owner_id"], "PC")
        assert pairing.pending("local") == []
        monkeypatch.setenv("SSL_CERT_FILE", cert)
        req = await client.begin(f"wss://localhost:{port}/connect", owner["owner_id"], "PC")
        assert req["state"] == "pending"
        other = PairingClient(tmp_path / "wrongname/pending.json")
        with pytest.raises(OSError, match="no downgrade"):
            await other.begin(f"wss://127.0.0.1:{port}/connect", owner["owner_id"], "PC")
        assert len(pairing.pending("local")) == 1
    store.close()


@pytest.mark.asyncio
async def test_pairing_rejects_cross_origin_redirect_before_any_secret(tmp_path):
    from websockets.asyncio.server import serve

    from acp_gateway.connectors.pairing_client import PairingClient

    seen = []

    async def destination(ws):
        seen.append(await ws.recv())

    async with serve(destination, "127.0.0.1", 0) as evil:
        location = f"ws://127.0.0.1:{evil.sockets[0].getsockname()[1]}/stolen"

        def redirect(connection, request):
            response = connection.respond(307, "redirect")
            response.headers["Location"] = location
            return response

        async with serve(destination, "127.0.0.1", 0, process_request=redirect) as server:
            client = PairingClient(tmp_path / "pending.json")
            with pytest.raises(ValueError, match="rejected"):
                await client.begin(
                    f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect",
                    "owner",
                    "PC",
                    allow_ws=True,
                )
    assert seen == []


@pytest.mark.asyncio
async def test_pairing_duplicate_json_members_rejected(tmp_path):
    from acp_gateway.pairing import PairingService

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    owner = pairing.bootstrap([])
    dispatcher = ControlDispatcher(store.computers, pairing=pairing)
    async with dispatcher.listen("127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect/pair"
        async with connect(url, proxy=None) as ws:
            payload = json.dumps(
                {
                    "action": "request",
                    "owner_id": owner["owner_id"],
                    "device_code": secrets.token_urlsafe(32),
                    "name": "PC",
                }
            )
            payload = payload[:-1] + ', "name": "different"}'
            await ws.send(payload)
            assert json.loads(await ws.recv()) == {"error": "pairing request rejected"}
        assert pairing.pending("local") == []
    store.close()


@pytest.mark.asyncio
async def test_pairing_wire_limit_is_four_kib_before_json_parsing(tmp_path):
    from websockets.exceptions import ConnectionClosedError

    from acp_gateway.pairing import PairingService

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    dispatcher = ControlDispatcher(store.computers, pairing=pairing)
    async with (
        dispatcher.listen("127.0.0.1", 0) as server,
        connect(
            f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect/pair", proxy=None
        ) as ws,
    ):
        await ws.send("x" * 5000)
        with pytest.raises(ConnectionClosedError):
            await ws.recv()
        assert ws.close_code == 1009
    store.close()
