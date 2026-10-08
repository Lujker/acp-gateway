"""Real TLS and WebSocket lifecycle; all credentials/databases are temporary."""

import asyncio
import ssl
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from acp_gateway.agents.tls import pinned_context
from acp_gateway.connectors.control import (
    ConnectorAccessError,
    ControlDispatcher,
    connect_control,
    run_connector,
)
from acp_gateway.connectors.protocol import (
    AgentManifest,
    Hello,
    Ping,
    Pong,
    decode_frame,
    encode_frame,
)
from acp_gateway.storage import Store
from fakes.certs import fingerprint, make_cert


@pytest.fixture
async def ingress(tmp_path):
    cert, key = make_cert(tmp_path)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    client_tls = pinned_context(ssl.PEM_cert_to_DER_cert(Path(cert).read_text()))
    store = Store.open_in(tmp_path / "data")
    token_file = tmp_path / "computer.key"
    store.computers.issue("work", token_file, display_name="Work")
    credential = token_file.read_text().strip()
    dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5)
    async with dispatcher.listen("127.0.0.1", 0, tls=tls) as server:
        port = server.sockets[0].getsockname()[1]
        yield (
            dispatcher,
            store.computers,
            f"wss://127.0.0.1:{port}/connect",
            credential,
            fingerprint(cert),
            client_tls,
        )
    store.close()


def hello(identity="work"):
    return Hello(computer_id=identity, agents=[AgentManifest(alias="goose", display_name="Goose")])


async def raw_connect(ingress, *, identity="work", credential=None, **kwargs):
    _, _, url, token, _, tls = ingress
    return await connect(
        url,
        ssl=tls,
        proxy=None,
        additional_headers={
            "Authorization": "Bearer " + (credential if credential is not None else token),
            "X-ACP-Computer": identity,
        },
        **kwargs,
    )


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_registration_heartbeat_cleanup_and_new_epoch(ingress):
    dispatcher, _, url, credential, pin, _ = ingress
    connection, welcome = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    assert dispatcher.active["work"].agents[0].alias == "goose"
    ping = decode_frame(await connection.recv())
    assert isinstance(ping, Ping)
    await connection.send(encode_frame(Pong(sequence=ping.sequence)))
    await connection.close()
    await eventually(lambda: not dispatcher.active)
    next_connection, next_welcome = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    assert welcome.connection_id != next_welcome.connection_id
    await next_connection.close()


async def test_persisted_diagnostics_survive_listener_restart_without_claiming_online(ingress):
    dispatcher, registry, url, credential, pin, _ = ingress
    connection, welcome = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    await eventually(lambda: dispatcher.status()[0]["connected"])
    row = dispatcher.status()[0]
    assert row["epoch"] == str(welcome.connection_id) and row["connected_at"]
    await connection.close()
    await eventually(lambda: not dispatcher.active)
    row = dispatcher.status()[0]
    assert not row["connected"] and row["disconnect_reason"] == "connection_closed"
    restarted = ControlDispatcher(registry)
    assert restarted.status()[0]["connected_at"] == row["connected_at"]
    assert not restarted.status()[0]["connected"]


@pytest.mark.parametrize("identity,credential", [("other", None), ("work", "incorrect")])
async def test_bad_credential_rejected_before_upgrade(ingress, identity, credential):
    with pytest.raises(InvalidStatus) as error:
        await raw_connect(ingress, identity=identity, credential=credential)
    assert error.value.response.status_code == 401
    assert not ingress[0].active


async def test_ingress_connection_limit_rejects_new_handshakes(ingress):
    dispatcher = ingress[0]
    dispatcher.max_connections = 1
    connection, _ = await connect_control(
        ingress[2], credential=ingress[3], hello=hello(), fingerprint=ingress[4]
    )
    try:
        with pytest.raises(InvalidStatus) as error:
            await raw_connect(ingress)
        assert error.value.response.status_code == 503
        assert dispatcher.active["work"].connection is not None
    finally:
        await connection.close()


async def test_hello_cannot_impersonate_another_computer(ingress):
    connection = await raw_connect(ingress)
    await connection.send(encode_frame(hello("other")))
    await connection.wait_closed()
    assert connection.close_code == 1008
    assert not ingress[0].active


async def test_new_authenticated_connection_replaces_active_registration(ingress):
    dispatcher, _, url, credential, pin, _ = ingress
    first, welcome = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    second, next_welcome = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    try:
        await asyncio.wait_for(first.wait_closed(), timeout=3)
        assert first.close_code == 1008
        assert next_welcome.connection_id != welcome.connection_id
        assert dispatcher.active["work"].connection_id == next_welcome.connection_id
        assert dispatcher.status()[0]["epoch"] == str(next_welcome.connection_id)
        assert dispatcher.status()[0]["disconnect_reason"] is None
        # The replaced handler's cleanup must not remove the new registration.
        await asyncio.sleep(0.05)
        assert dispatcher.active["work"].connection_id == next_welcome.connection_id
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("action", ["rotate", "revoke"])
async def test_credential_change_closes_live_connection(ingress, tmp_path, action):
    dispatcher, registry, url, credential, pin, _ = ingress
    connection, _ = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    if action == "rotate":
        registry.issue("work", tmp_path / "rotated.key")
    else:
        registry.revoke("work")
    async with asyncio.timeout(3):
        await connection.wait_closed()
    assert connection.close_code == 1008
    await eventually(lambda: not dispatcher.active)
    with pytest.raises(ValueError, match="cannot connect"):
        await connect_control(url, credential=credential, hello=hello(), fingerprint=pin)


async def test_rotation_between_upgrade_and_hello_is_refused(ingress, tmp_path):
    connection = await raw_connect(ingress)
    ingress[1].issue("work", tmp_path / "rotated.key")
    await connection.send(encode_frame(hello()))
    await connection.wait_closed()
    assert connection.close_code == 1008
    assert not ingress[0].active


async def test_wrong_heartbeat_sequence_closes_connection(ingress):
    _, _, url, credential, pin, _ = ingress
    connection, _ = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    ping = decode_frame(await connection.recv())
    await connection.send(encode_frame(Pong(sequence=ping.sequence + 1)))
    await connection.wait_closed()
    assert connection.close_code == 1008


async def test_native_keepalive_does_not_require_application_heartbeat(ingress):
    dispatcher, _, url, credential, pin, _ = ingress
    connection, _ = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    try:
        await asyncio.sleep(5.2)
        assert dispatcher.active["work"].connection is not None
        assert connection.close_code is None
    finally:
        await connection.close()


async def test_pin_mismatch_sends_no_authorization_header(ingress, monkeypatch):
    dispatcher, _, url, credential, _, _ = ingress
    seen = []
    original = dispatcher.registry.authorize

    def capture(identity, token):
        seen.append(True)
        return original(identity, token)

    monkeypatch.setattr(dispatcher.registry, "authorize", capture)
    with pytest.raises(ValueError, match="verify TLS pin"):
        await connect_control(
            url, credential=credential, hello=hello(), fingerprint="00:" * 31 + "00"
        )
    assert seen == []


async def test_connector_runs_heartbeats_and_cancels_cleanly(ingress, capsys):
    dispatcher, _, url, credential, pin, _ = ingress
    task = asyncio.create_task(
        run_connector(url, credential=credential, hello=hello(), fingerprint=pin)
    )
    try:
        await eventually(lambda: "work" in dispatcher.active)
        await asyncio.sleep(0.1)
        assert credential not in capsys.readouterr().out
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await eventually(lambda: not dispatcher.active)


async def test_connector_retries_network_drop_but_stops_after_revocation(ingress):
    dispatcher, registry, url, credential, pin, _ = ingress
    task = asyncio.create_task(
        run_connector(url, credential=credential, hello=hello(), fingerprint=pin)
    )
    try:
        await eventually(lambda: "work" in dispatcher.active)
        previous = dispatcher.active["work"]
        await previous.connection.close(code=1012, reason="test restart")
        await eventually(
            lambda: "work" in dispatcher.active and dispatcher.active["work"] is not previous
        )
        registry.revoke("work")
        with pytest.raises(ConnectorAccessError):
            await asyncio.wait_for(task, 3)
        await asyncio.sleep(0.05)
        assert not dispatcher.active
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_binary_and_oversized_frames_rejected(ingress):
    for payload in (b"invalid", "x" * 65_537):
        connection = await raw_connect(ingress)
        await connection.send(payload)
        await connection.wait_closed()
        assert connection.close_code in (1008, 1009)
    assert not ingress[0].active


async def test_wire_debug_does_not_log_credentials(ingress, caplog):
    import logging

    _, _, url, credential, pin, _ = ingress
    with caplog.at_level(logging.DEBUG):
        connection, _ = await connect_control(
            url, credential=credential, hello=hello(), fingerprint=pin
        )
        await connection.close()
    assert credential not in caplog.text


async def test_duplicate_authentication_headers_refused(ingress):
    _, _, url, credential, _, tls = ingress
    with pytest.raises(InvalidStatus) as error:
        await connect(
            url,
            ssl=tls,
            proxy=None,
            additional_headers=[
                ("Authorization", "Bearer " + credential),
                ("Authorization", "Bearer " + credential),
                ("X-ACP-Computer", "work"),
            ],
        )
    assert error.value.response.status_code == 401


async def test_browser_origin_refused(ingress):
    with pytest.raises(InvalidStatus) as error:
        await raw_connect(ingress, origin="https://example.invalid")
    assert error.value.response.status_code == 403
    assert not ingress[0].active


async def test_another_computer_stays_connected_on_revocation(ingress, tmp_path):
    dispatcher, registry, url, credential, pin, _ = ingress
    registry.issue("home", tmp_path / "home.key", display_name="Home")
    other_token = (tmp_path / "home.key").read_text().strip()
    first, _ = await connect_control(url, credential=credential, hello=hello(), fingerprint=pin)
    other, _ = await connect_control(
        url, credential=other_token, hello=hello("home"), fingerprint=pin
    )
    try:
        registry.revoke("work")
        async with asyncio.timeout(3):
            await first.wait_closed()
        assert "home" in dispatcher.active
        ping = decode_frame(await other.recv())
        await other.send(encode_frame(Pong(sequence=ping.sequence)))
    finally:
        await first.close()
        await other.close()


async def test_access_database_failure_closes_active_connection(ingress, monkeypatch):
    import sqlite3

    dispatcher, _, url, credential, pin, _ = ingress
    connection, _ = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )

    def fail(identity, generation):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(dispatcher.registry, "grant_valid", fail)
    async with asyncio.timeout(3):
        await connection.wait_closed()
    assert connection.close_code == 1011
    await eventually(lambda: not dispatcher.active)


async def test_non_websocket_tls_endpoint_reports_safe_error(tmp_path):
    cert, key = make_cert(tmp_path)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)

    async def not_websocket(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"garbage\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(not_websocket, "127.0.0.1", 0, ssl=tls)
    async with server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(ValueError, match="cannot connect to dispatcher"):
            await connect_control(
                f"wss://127.0.0.1:{port}/connect",
                credential="acpc_" + "a" * 43,
                hello=hello(),
                fingerprint=fingerprint(cert),
            )


@pytest.fixture(params=["/connect", "/gateway/connect", "/"])
async def plain_ingress(tmp_path, request):
    store = Store.open_in(tmp_path / "data")
    key = tmp_path / "computer.key"
    store.computers.issue("work", key, display_name="Work")
    dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5, connect_path=request.param)
    try:
        async with dispatcher.listen("127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            yield dispatcher, f"ws://127.0.0.1:{port}{request.param}", key.read_text().strip()
    finally:
        store.close()


async def test_plaintext_ip_registration_and_heartbeat(plain_ingress, monkeypatch):
    dispatcher, url, credential = plain_ingress
    if dispatcher.connect_path == "/":
        url = url.removesuffix("/")  # bare IP:port is a root-path WebSocket URL

    async def no_tls_probe(*args, **kwargs):
        raise AssertionError("plaintext must not probe TLS")

    monkeypatch.setattr("acp_gateway.connectors.control.pin_certificate", no_tls_probe)
    connection, welcome = await connect_control(url, credential=credential, hello=hello())
    try:
        assert dispatcher.active["work"].connection_id == welcome.connection_id
        assert connection.transport.get_extra_info("ssl_object") is None
        ping = decode_frame(await connection.recv())
        await connection.send(encode_frame(Pong(sequence=ping.sequence)))
        assert connection.state.name != "CLOSED"
    finally:
        await connection.close()
    await eventually(lambda: not dispatcher.active)


async def test_plaintext_still_requires_computer_credential(plain_ingress):
    dispatcher, url, _ = plain_ingress
    with pytest.raises(ValueError, match="cannot connect to dispatcher"):
        await connect_control(url, credential="invalid", hello=hello())
    assert not dispatcher.active


async def test_listener_requires_its_configured_path(plain_ingress):
    dispatcher, url, credential = plain_ingress
    wrong_url = url[: url.index("/", len("ws://"))] + "/wrong"
    with pytest.raises(InvalidStatus) as error:
        await connect(
            wrong_url,
            proxy=None,
            additional_headers={"Authorization": "Bearer " + credential, "X-ACP-Computer": "work"},
        )
    assert error.value.response.status_code == 404
    assert not dispatcher.active


async def test_plaintext_revoke_closes_only_registered_connection(plain_ingress):
    dispatcher, url, credential = plain_ingress
    connection, _ = await connect_control(url, credential=credential, hello=hello())
    dispatcher.registry.revoke("work")
    await asyncio.wait_for(connection.wait_closed(), timeout=3)
    assert connection.close_code == 1008
    await eventually(lambda: not dispatcher.active)


async def test_pin_cannot_be_silently_ignored_for_plaintext(plain_ingress):
    dispatcher, url, credential = plain_ingress
    with pytest.raises(ValueError, match="requires a wss"):
        await connect_control(url, credential=credential, hello=hello(), fingerprint="00" * 32)
    assert not dispatcher.active


async def test_invalid_hello_does_not_displace_live_registration(ingress):
    dispatcher, _, url, credential, pin, _ = ingress
    first, welcome = await connect_control(
        url, credential=credential, hello=hello(), fingerprint=pin
    )
    invalid = await raw_connect(ingress)
    try:
        await invalid.send(encode_frame(hello("other")))
        await asyncio.wait_for(invalid.wait_closed(), timeout=3)
        assert invalid.close_code == 1008
        assert dispatcher.active["work"].connection_id == welcome.connection_id
        ping = decode_frame(await first.recv())
        await first.send(encode_frame(Pong(sequence=ping.sequence)))
    finally:
        await first.close()
        await invalid.close()


async def test_wss_without_pin_rejects_untrusted_certificate_before_auth(ingress, monkeypatch):
    dispatcher, _, url, credential, _, _ = ingress
    seen = []
    original = dispatcher.registry.authorize

    def capture(identity, token):
        seen.append(True)
        return original(identity, token)

    monkeypatch.setattr(dispatcher.registry, "authorize", capture)
    with pytest.raises(ValueError, match="cannot connect to dispatcher"):
        await connect_control(url, credential=credential, hello=hello())
    assert seen == []


async def test_ca_validated_domain_and_subpath_without_pin(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, ca=True, hostname="localhost")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    trusted = ssl.create_default_context(cafile=cert)
    monkeypatch.setattr(
        "acp_gateway.connectors.control.ssl.create_default_context", lambda: trusted
    )
    store = Store.open(":memory:")
    token = tmp_path / "computer.key"
    store.computers.issue("work", token, display_name="Work")
    dispatcher = ControlDispatcher(store.computers, connect_path="/gateway/connect")
    try:
        async with dispatcher.listen("127.0.0.1", 0, tls=tls) as server:
            port = server.sockets[0].getsockname()[1]
            connection, _ = await connect_control(
                f"wss://localhost:{port}/gateway/connect",
                credential=token.read_text().strip(),
                hello=hello(),
            )
            await connection.close()
    finally:
        store.close()
