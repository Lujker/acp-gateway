"""Negotiated read-only management over real WS; no ACP or updater invocation."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.protocol import (
    AgentManifest,
    Hello,
    InstallationReport,
    UpdateInfoRequest,
    encode_frame,
)
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.storage import Store


def report():
    return InstallationReport(
        installed_version="0.2.0", manager="uv", source="registry", platform="win32"
    )


@asynccontextmanager
async def connected(
    tmp_path,
    *,
    offered=True,
    old_dispatcher=False,
    local_report=report,
    operations=False,
    update_handler=None,
    profiles=False,
    discovery=False,
    profiles_handler=None,
    shell_operations=False,
    shell_handler=None,
    agents=None,
):
    store = Store.open_in(tmp_path / "data")
    key = tmp_path / "pc.key"
    store.computers.issue("home", key, display_name="Home")
    dispatcher = ControlDispatcher(store.computers)
    if old_dispatcher:
        authenticate = dispatcher.authenticate

        def old_auth(connection, request):
            response = authenticate(connection, request)
            connection.update_inventory = False
            connection.update_operations = False
            connection.shell_operations = False
            return response

        dispatcher.authenticate = old_auth
    async with dispatcher.listen("127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"
        manifests = agents or [AgentManifest(alias="hermes", display_name="H")]
        connection, welcome = await connect_control(
            url,
            credential=key.read_text().strip(),
            hello=Hello(computer_id="home", agents=manifests),
            update_inventory=offered,
            update_operations=operations,
            agent_profiles=profiles,
            agent_discovery=discovery,
            shell_operations=shell_operations,
        )

        async def factory(alias):
            pytest.fail("inventory must not open an ACP session")

        peer = RelayPeer(
            connection,
            welcome.connection_id,
            [a.alias for a in manifests],
            local_factory=factory,
            update_inventory=welcome.update_inventory == 1,
            installation_report=local_report,
            update_operations=welcome.update_operations == 1,
            update_handler=update_handler,
            agent_profiles=welcome.agent_profiles == 1,
            agent_discovery=welcome.agent_discovery == 1,
            profiles_handler=profiles_handler,
            shell_operations=welcome.shell_operations == 1,
            shell_handler=shell_handler,
        )
        task = asyncio.create_task(peer.run())
        await peer.ready.wait()
        try:
            yield dispatcher, peer, welcome
        finally:
            await connection.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    store.close()


async def test_negotiated_report_is_typed_and_does_not_open_agent(tmp_path):
    async with connected(tmp_path) as (dispatcher, peer, welcome):
        assert welcome.update_inventory == 1
        assert await dispatcher.update_info("home") == report()
        assert not peer.streams
        assert not dispatcher.active["home"].relay.streams


@pytest.mark.parametrize("offered,old", [(False, False), (True, True)])
async def test_old_peers_continue_agent_registration_without_management(tmp_path, offered, old):
    async with connected(tmp_path, offered=offered, old_dispatcher=old) as (dispatcher, _, welcome):
        assert welcome.update_inventory is None
        assert "update_inventory" not in encode_frame(welcome)
        with pytest.raises(AgentUnavailable, match="unsupported"):
            await dispatcher.update_info("home")
        assert dispatcher.status()[0]["connected"]


async def test_inventory_after_revoke_is_refused(tmp_path):
    async with connected(tmp_path) as (dispatcher, _, _):
        dispatcher.registry.revoke("home")
        with pytest.raises(AgentUnavailable):
            await dispatcher.update_info("home")


async def test_wrong_epoch_management_frame_closes_connection(tmp_path):
    async with connected(tmp_path) as (_, peer, _):
        await peer.send(UpdateInfoRequest(epoch=uuid4(), request=uuid4()))
        async with asyncio.timeout(5):
            await peer.closed.wait()
        assert peer.connection.close_code == 1008


async def test_unnegotiated_frame_is_rejected_without_running_handler(tmp_path):
    async with connected(tmp_path, offered=False) as (_, peer, welcome):
        await peer.send(UpdateInfoRequest(epoch=welcome.connection_id, request=uuid4()))
        async with asyncio.timeout(5):
            await peer.closed.wait()
        assert peer.connection.close_code == 1008


async def test_failed_report_is_unavailable_without_leaking_exception(tmp_path):
    def failing_report():
        raise PermissionError("private installation details")

    async with connected(tmp_path, local_report=failing_report) as (dispatcher, peer, _):
        assert await dispatcher.update_info("home") is None
        assert not peer.closed.is_set()


async def test_inflight_report_is_fenced_when_access_is_revoked(tmp_path):
    import threading

    started, release = threading.Event(), threading.Event()

    def slow_report():
        started.set()
        release.wait(timeout=5)
        return report()

    async with connected(tmp_path, local_report=slow_report) as (dispatcher, _, _):
        task = asyncio.create_task(dispatcher.update_info("home"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            dispatcher.registry.revoke("home")
            with pytest.raises((AgentUnavailable, ConnectionError)):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_late_timeout_response_cannot_resolve_another_request(tmp_path):
    import threading

    started, release = threading.Event(), threading.Event()
    calls = 0

    def slow_first():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            release.wait(timeout=5)
            return report()
        return report().model_copy(update={"installed_version": "0.3.0"})

    async with connected(tmp_path, local_report=slow_first) as (dispatcher, _, _):
        relay = dispatcher.active["home"].relay
        first = asyncio.create_task(relay.update_info(timeout=0.1))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            with pytest.raises(TimeoutError):
                await first
            assert (await relay.update_info()).installed_version == "0.3.0"
            release.set()
            assert (await relay.update_info()).installed_version == "0.3.0"
            assert not relay.update_requests
            assert calls == 3
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


async def test_inventory_only_peer_cannot_accidentally_gain_update_capability(tmp_path):
    async with connected(
        tmp_path, local_report=lambda: report().model_copy(update={"can_update": True})
    ) as (dispatcher, _, welcome):
        assert welcome.update_operations is None
        assert not (await dispatcher.update_info("home")).can_update


async def test_negotiated_operation_and_status_have_epoch_and_operation_identity(tmp_path):
    import time

    from acp_gateway.connectors.protocol import UpdateOperation, UpdateRequest

    calls = []

    def handler(request):
        calls.append(request.action)
        return UpdateOperation(operation=request.operation, status="queued")

    async with connected(tmp_path, operations=True, update_handler=handler) as (
        dispatcher,
        _,
        welcome,
    ):
        binding = f"{welcome.connection_id}:{dispatcher.active['home'].generation}"
        request = UpdateRequest(
            epoch=uuid4(),
            request=uuid4(),
            operation=uuid4(),
            action="execute",
            actor="owner",
            target_version="0.3.0",
            current_version="0.2.0",
            expires=float(time.time() + 60),
        )
        assert (
            await dispatcher.update_operation("home", binding, request)
        ).operation == request.operation
        old_binding = f"{uuid4()}:{dispatcher.active['home'].generation}"
        with pytest.raises(AgentUnavailable):
            await dispatcher.update_operation("home", old_binding, request)
        status = request.model_copy(update={"action": "status", "request": uuid4()})
        assert (await dispatcher.update_operation("home", old_binding, status)).status == "queued"
        assert calls == ["execute", "status"]
