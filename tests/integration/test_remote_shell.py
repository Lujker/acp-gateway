"""Real WS shell management: no model streams, no replay after loss, own result windows."""

import asyncio
import secrets
import shlex
import sys
import time
from uuid import uuid4

import pytest
from test_connector_update_inventory import connected

from acp_gateway.adapters.managed_sessions import ManagedSessions
from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.control import connect_control, run_connector
from acp_gateway.connectors.maintenance import ConnectorMaintenance
from acp_gateway.connectors.protocol import (
    AgentManifest,
    Hello,
    ShellRequest,
    ShellResponse,
    ShellResult,
)
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.connectors.shell_service import LocalShell
from acp_gateway.log import MASK, register_secret


def request(action="status", **kwargs):
    return ShellRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=kwargs.pop("operation", uuid4()),
        action=action,
        actor=kwargs.pop("actor", "telegram:123"),
        expires=time.time() + 30,
        **kwargs,
    )


@pytest.fixture
async def local(tmp_path):
    if sys.platform != "linux":
        pytest.skip("real local shell Linux supervisor")
    profiles = [
        AgentProfile(
            alias="code",
            kind="codex",
            backend="codex",
            default_cwd=str(tmp_path),
            codex={"mode": "work"},
        )
    ]
    bindings = ManagedSessions(tmp_path / "bindings", profiles[0])
    bindings.create("owned", str(tmp_path))
    maintenance = ConnectorMaintenance(profiles, {})
    service = LocalShell(
        tmp_path / "shell.db",
        profiles=lambda: profiles,
        authorize=lambda alias, sid, cwd: bindings.contains(sid, cwd),
        admission=maintenance.check_admission,
    )
    maintenance.services.append(service)
    try:
        yield service, profiles, maintenance
    finally:
        await service.close()
        bindings.close()


def start(tmp_path, **kwargs):
    return request(
        "start",
        alias="code",
        session_id="owned",
        cwd=str(tmp_path),
        command=kwargs.pop("command", "printf once >> count.txt; printf done"),
        **kwargs,
    )


async def finish(dispatcher, operation):
    async with asyncio.timeout(10):
        while True:
            result = await dispatcher.shell("home", request(operation=operation))
            if result.state not in {"reserved", "running"}:
                return result
            await asyncio.sleep(0.01)


async def test_negotiated_execution_and_duplicate_uuid_do_not_open_model_streams(tmp_path, local):
    service, _, _ = local
    async with connected(
        tmp_path / "wire",
        shell_operations=True,
        shell_handler=service.request,
        agents=[AgentManifest(alias="code", display_name="Code")],
    ) as (dispatcher, peer, welcome):
        assert welcome.shell_operations == 1 and dispatcher.status()[0]["shell_operations"]
        frame = start(tmp_path)
        assert (await dispatcher.shell("home", frame)).state == "running"
        result = await finish(dispatcher, frame.operation)
        assert result.state == "completed" and result.output == "done" and result.exit_code == 0
        assert result.cleanup_verified and result.interpreter == "sh"
        assert (
            await dispatcher.shell("home", frame.model_copy(update={"request": uuid4()}))
        ).state == "completed"
        assert (tmp_path / "count.txt").read_text() == "once"
        assert not peer.streams and not dispatcher.active["home"].relay.streams


@pytest.mark.parametrize("offered,old", [(False, False), (True, True)])
async def test_old_peers_refuse_before_execution(tmp_path, offered, old):
    async with connected(tmp_path, shell_operations=offered, old_dispatcher=old) as (
        dispatcher,
        _,
        welcome,
    ):
        assert welcome.shell_operations is None
        with pytest.raises(AgentUnavailable, match="unavailable"):
            await dispatcher.shell("home", request())


async def test_safe_profile_and_foreign_actor_are_refused_and_cancel_is_scoped(tmp_path, local):
    service, profiles, maintenance = local
    async with connected(
        tmp_path / "wire",
        shell_operations=True,
        shell_handler=service.request,
        agents=[AgentManifest(alias="code", display_name="Code")],
    ) as (dispatcher, _, _):
        original = profiles[0]
        profiles[0] = original.model_copy(
            update={"kind": "goose", "backend": "remote", "codex": None}
        )
        assert (await dispatcher.shell("home", start(tmp_path))).reason == "profile_unavailable"
        profiles[0] = original
        profiles[0] = profiles[0].model_copy(
            update={"codex": profiles[0].codex.model_copy(update={"mode": "safe"})}
        )
        assert (await dispatcher.shell("home", start(tmp_path))).reason == "safe_profile"
        profiles[0] = profiles[0].model_copy(
            update={"codex": profiles[0].codex.model_copy(update={"mode": "work"})}
        )
        frame = start(tmp_path, command="sleep 30")
        await dispatcher.shell("home", frame)
        assert maintenance.status()["running_sessions"] == 1
        with pytest.raises(ValueError, match="busy"):
            maintenance.apply("prepare")
        denied = await dispatcher.shell(
            "home", request("cancel", operation=frame.operation, actor="telegram:456")
        )
        assert denied.state == "refused" and denied.reason == "not_found"
        assert (
            await dispatcher.shell("home", request("cancel", operation=frame.operation))
        ).state == "running"
        result = await finish(dispatcher, frame.operation)
        assert result.state == "cancelled" and result.cleanup_verified
        token = maintenance.apply("prepare")["token"]
        assert (await dispatcher.shell("home", start(tmp_path))).reason == "maintenance"
        maintenance.apply("resume", token)


async def test_result_windows_are_complete_and_actor_owned(tmp_path, local):
    service, _, _ = local
    async with connected(
        tmp_path / "wire",
        shell_operations=True,
        shell_handler=service.request,
        agents=[AgentManifest(alias="code", display_name="Code")],
    ) as (dispatcher, _, _):
        code = "import sys;sys.stdout.write('abc'*3000)"
        frame = start(tmp_path, command=shlex.quote(sys.executable) + " -c " + shlex.quote(code))
        await dispatcher.shell("home", frame)
        first = await finish(dispatcher, frame.operation)
        assert first.total == 9000 and len(first.output) == 4000
        denied = await dispatcher.shell(
            "home", request(operation=frame.operation, actor="telegram:456")
        )
        assert denied.reason == "not_found" and not denied.output
        text = first.output
        for offset in (4000, 8000):
            part = await dispatcher.shell("home", request(operation=frame.operation, offset=offset))
            text += part.output
        assert text == "abc" * 3000


async def test_expired_start_is_refused_without_creating_a_local_intent(tmp_path, local):
    service, _, _ = local
    async with connected(
        tmp_path / "wire",
        shell_operations=True,
        shell_handler=service.request,
        agents=[AgentManifest(alias="code", display_name="Code")],
    ) as (dispatcher, _, _):
        frame = start(tmp_path).model_copy(update={"expires": time.time() - 1})
        result = await dispatcher.shell("home", frame)
        assert result.state == "refused" and result.reason == "stale"
        assert not (tmp_path / "count.txt").exists()
        assert service.journal.conn.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


async def test_output_secret_is_redacted_before_wire_window_boundaries(tmp_path, local):
    service, _, _ = local
    credential = secrets.token_hex(32)
    register_secret(credential)
    code = f"import sys;sys.stdout.write('x'*3950+{credential!r}+'z'*100)"
    async with connected(
        tmp_path / "wire",
        shell_operations=True,
        shell_handler=service.request,
        agents=[AgentManifest(alias="code", display_name="Code")],
    ) as (dispatcher, _, _):
        frame = start(tmp_path, command=shlex.quote(sys.executable) + " -c " + shlex.quote(code))
        await dispatcher.shell("home", frame)
        first = await finish(dispatcher, frame.operation)
        second = await dispatcher.shell("home", request(operation=frame.operation, offset=4000))
        output = first.output + second.output
        assert output == "x" * 3950 + MASK + "z" * 100
        assert credential not in output and credential[:32] not in output


async def test_reconnect_loop_wires_shell_handler_and_stops_without_model_activity(tmp_path, local):
    service, _, _ = local
    manifests = [AgentManifest(alias="code", display_name="Code")]
    wire = tmp_path / "runtime"
    async with connected(wire, agents=manifests) as (dispatcher, peer, welcome):
        runtime = asyncio.create_task(
            run_connector(
                f"ws://127.0.0.1:{peer.connection.remote_address[1]}/connect",
                credential=(wire / "pc.key").read_text().strip(),
                hello=Hello(computer_id="home", agents=manifests),
                shell_handler=service.request,
            )
        )
        try:
            async with asyncio.timeout(5):
                while True:
                    current = dispatcher.active.get("home")
                    if (
                        current
                        and current.connection_id != welcome.connection_id
                        and current.relay.ready.is_set()
                    ):
                        break
                    await asyncio.sleep(0.01)
            frame = start(tmp_path)
            await dispatcher.shell("home", frame)
            assert (await finish(dispatcher, frame.operation)).state == "completed"
            assert not current.relay.streams
        finally:
            runtime.cancel()
            await asyncio.gather(runtime, return_exceptions=True)


async def test_shell_rpc_quota_does_not_block_update_inventory(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def handler(frame):
        nonlocal count
        count += 1
        if count == 4:
            started.set()
        await release.wait()
        return ShellResult(operation=frame.operation, state="running")

    async with connected(tmp_path, shell_operations=True, shell_handler=handler) as (
        dispatcher,
        peer,
        _,
    ):
        tasks = [asyncio.create_task(dispatcher.shell("home", request())) for _ in range(4)]
        try:
            await asyncio.wait_for(started.wait(), 5)
            with pytest.raises(AgentUnavailable, match="busy"):
                await dispatcher.shell("home", request())
            assert (await dispatcher.update_info("home")).installed_version == "0.2.0"
            assert len(peer.shell_workers) == 4 and not peer.update_workers
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_wrong_epoch_and_operation_response_cannot_settle_another_request(tmp_path):
    async with connected(tmp_path / "epoch", shell_operations=True) as (_, peer, _):
        await peer.send(request())
        await asyncio.wait_for(peer.closed.wait(), 5)
        assert peer.connection.close_code == 1008

    holder = {}

    async def handler(frame):
        peer = holder["peer"]
        await peer.send(
            ShellResponse(
                epoch=peer.epoch,
                request=frame.request,
                result=ShellResult(operation=uuid4(), state="running"),
            )
        )
        return ShellResult(operation=frame.operation, state="running")

    async with connected(tmp_path / "operation", shell_operations=True, shell_handler=handler) as (
        dispatcher,
        peer,
        _,
    ):
        holder["peer"] = peer
        with pytest.raises((ConnectionError, AgentUnavailable)):
            await dispatcher.shell("home", request())


async def test_lost_start_response_can_be_inspected_after_reconnect_without_replay(tmp_path, local):
    service, _, _ = local
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(frame):
        result = await service.request(frame)
        if frame.action == "start":
            entered.set()
            await release.wait()
        return result

    wire = tmp_path / "wire"
    manifests = [AgentManifest(alias="code", display_name="Code")]
    async with connected(wire, shell_operations=True, shell_handler=handler, agents=manifests) as (
        dispatcher,
        peer,
        _,
    ):
        port = peer.connection.remote_address[1]
        frame = start(tmp_path, command="printf once >> count.txt; sleep 0.3; printf done")
        pending = asyncio.create_task(dispatcher.shell("home", frame))
        await asyncio.wait_for(entered.wait(), 5)
        await peer.connection.close(code=1011)
        with pytest.raises((ConnectionError, AgentUnavailable)):
            await pending
        connection, welcome = await connect_control(
            f"ws://127.0.0.1:{port}/connect",
            credential=(wire / "pc.key").read_text().strip(),
            hello=Hello(computer_id="home", agents=manifests),
            shell_operations=True,
        )

        async def no_model(alias):
            pytest.fail("shell must not initialize an agent")

        fresh = RelayPeer(
            connection,
            welcome.connection_id,
            ["code"],
            local_factory=no_model,
            shell_operations=True,
            shell_handler=service.request,
        )
        runner = asyncio.create_task(fresh.run())
        try:
            await fresh.ready.wait()
            result = await finish(dispatcher, frame.operation)
            assert result.state == "completed" and result.output == "done"
            assert (
                await dispatcher.shell("home", frame.model_copy(update={"request": uuid4()}))
            ).state == "completed"
            assert (tmp_path / "count.txt").read_text() == "once"
        finally:
            release.set()
            await connection.close()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
