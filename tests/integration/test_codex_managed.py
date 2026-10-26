"""End-to-end ACP client over a real, model-free native subprocess."""

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import acp_gateway.adapters.codex as codex_adapter
from acp_gateway.adapters.codex import CodexAcpTransport, CodexProcessManager
from acp_gateway.agents.client import AgentClient
from acp_gateway.agents.errors import AgentError, SessionNotFound
from acp_gateway.agents.events import MessageChunk, ThoughtChunk
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.launch import ConnectorLaunch, local_policies
from acp_gateway.connectors.policy import PolicyTransport
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.storage import Store


@pytest.fixture
def peer(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / "fakes" / "codex_peer.py"

    def make(scenario="happy", **settings):
        script = tmp_path / "codex-peer"
        script.write_text(
            "#!"
            + sys.executable
            + "\n"
            + source.read_text().replace('SCENARIO = "happy"', f"SCENARIO = {scenario!r}")
        )
        script.chmod(0o700)
        executable = str(script)
        if sys.platform == "win32":
            # Windows cannot exec a shebang script. Replace only the executable
            # launcher, keeping real Proactor subprocess pipes and native codec.
            executable = sys.executable
            spawn = asyncio.create_subprocess_exec

            async def launch(name, *args, **kwargs):
                assert name == sys.executable
                assert args == ("app-server", "--listen", "stdio://")
                return await spawn(sys.executable, str(script), *args, **kwargs)

            monkeypatch.setattr(
                codex_adapter,
                "asyncio",
                SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": launch}),
            )
        profile = AgentProfile(
            alias="code",
            kind="codex",
            backend="codex",
            default_cwd=str(tmp_path),
            codex={"executable": executable, "request_timeout": 1, "turn_timeout": 1, **settings},
        )
        return AgentClient(profile, pin_dir=tmp_path, request_timeout=2, reconnect_delays=())

    return make


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "codex-peer.log").read_text().splitlines()]


async def test_message_phases_and_public_summaries_are_separate(peer):
    client = peer("phases")
    try:
        sid = await client.new_session()
        events = [event async for event in client.prompt(sid, "hello")]
        answer = "".join(e.text for e in events if isinstance(e, MessageChunk))
        work = "".join(e.text for e in events if isinstance(e, ThoughtChunk))
        assert answer == "First result\n\nFinal result"
        assert "Checked files" in work and work.count("Public summary") == 1
        assert "RAW PRIVATE REASONING" not in answer + work
        assert "Checking files" not in answer and "Checked files" not in answer
    finally:
        await client.close()


async def test_unclassified_message_buffer_is_bounded_and_never_becomes_an_answer(peer):
    client = peer("pending-overflow")
    try:
        sid = await client.new_session()
        with pytest.raises(AgentError):
            await client.ask(sid, "hello")
        assert client.session_availability(sid) == "uncertain"
    finally:
        await client.close()


async def test_work_profile_uses_native_auto_review_and_network(peer, tmp_path):
    client = peer("work", mode="work")
    try:
        sid = await client.new_session()
        assert (await client.ask(sid, "hello")).text == "Hello world"
    finally:
        await client.close()
    start = next(f["params"] for f in calls(tmp_path) if f.get("method") == "thread/start")
    turn = next(f["params"] for f in calls(tmp_path) if f.get("method") == "turn/start")
    assert start["approvalPolicy"] == turn["approvalPolicy"] == "on-request"
    assert start["approvalsReviewer"] == turn["approvalsReviewer"] == "auto_review"
    assert start["sandbox"] == "workspace-write"
    assert start["ephemeral"] is False
    assert turn["sandboxPolicy"]["networkAccess"] is True


async def test_native_interrupt_confirms_cancel_and_keeps_chat_usable(peer, tmp_path):
    client = peer("interrupt", turn_timeout=3)
    try:
        sid = await client.new_session()
        task = asyncio.create_task(client.ask(sid, "hello"))
        for _ in range(100):
            if any(f.get("method") == "turn/start" for f in calls(tmp_path)):
                break
            await asyncio.sleep(0.01)
        await client.cancel(sid)
        assert (await asyncio.wait_for(task, 2)).stop_reason == "cancelled"
        assert client.session_availability(sid) == "attached"
        assert client._transport.uncertain == 0
    finally:
        await client.close()
    # Exact thread/turn interruption, never process termination as cancellation proof.
    assert sum(f.get("method") == "turn/interrupt" for f in calls(tmp_path)) == 1


async def test_unknown_turn_is_fenced_after_new_client_process(peer, tmp_path):
    client = peer("hold")
    sid = await client.new_session()
    task = asyncio.create_task(client.ask(sid, "hello"))
    for _ in range(100):
        if any(f.get("method") == "turn/start" for f in calls(tmp_path)):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await client.close()
    restored = peer()
    try:
        with pytest.raises(SessionNotFound):
            await restored.load_session(sid)
        assert not any(f.get("method") == "thread/resume" for f in calls(tmp_path))
    finally:
        await restored.close()


async def test_managed_codex_text_roundtrip_through_real_agent_client(peer, tmp_path):
    client = peer()
    try:
        sid = await client.new_session()
        reply = await asyncio.wait_for(client.ask(sid, "hello"), 3)
        assert reply.text == "Hello world"
        assert reply.stop_reason == "end_turn"
        assert client.supports_cancel
        assert client.agent_capabilities.load_session
        assert [s["sessionId"] for s in await client.list_sessions()] == [sid]
        transport = client._transport
    finally:
        await client.close()
    assert transport.process.returncode is not None
    assert [f["method"] for f in calls(tmp_path)] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]


@pytest.mark.parametrize("scenario", ["approval", "file-approval"])
@pytest.mark.parametrize("allow", [False, True])
async def test_native_approval_is_once_only_and_defaults_to_deny(peer, tmp_path, allow, scenario):
    client = peer(scenario)
    seen = []

    async def choose(request):
        seen.append(request)
        assert {o.kind for o in request.options} == {"allow_once", "reject_once"}
        return request.option("allow_once").option_id

    if allow:
        client.set_permission_handler(choose)
    try:
        sid = await client.new_session()
        reply = await asyncio.wait_for(client.ask(sid, "hello"), 3)
        assert reply.text == "Hello world"
        if allow:
            assert seen[0].raw_input["itemId"] == "tool-1"
            if scenario == "file-approval":
                assert seen[0].raw_input["changes"] == [
                    {
                        "path": str(tmp_path / "review.txt"),
                        "kind": {"type": "update", "move_path": None},
                        "diff": "@@ -1 +1 @@\n-old\n+reviewed\n",
                    }
                ]
            else:
                assert seen[0].raw_input["command"] == "echo safe"
    finally:
        await client.close()
    decisions = [f for f in calls(tmp_path) if f.get("id") == "approval-1" and "method" not in f]
    assert decisions == [
        {"id": "approval-1", "result": {"decision": "accept" if allow else "decline"}}
    ]


async def test_codex_closed_idle_dialogue_resumes_without_repeating_prompt(peer, tmp_path):
    client = peer()
    sid = await client.new_session()
    await client.close()
    assert client.session_availability(sid) == "restore-required"
    before = calls(tmp_path)
    try:
        await client.load_session(sid)
        assert client.session_availability(sid) == "attached"
        after = calls(tmp_path)[len(before) :]
        assert [f["method"] for f in after] == ["initialize", "initialized", "thread/resume"]
    finally:
        await client.close()


async def test_codex_never_attaches_arbitrary_ids_or_offers_cold_resume(peer, tmp_path):
    client = peer()
    try:
        with pytest.raises(SessionNotFound):
            await client.ask("someone-elses-thread", "hello")
        assert not client.connected
        assert not (tmp_path / "codex-peer.log").exists()
    finally:
        await client.close()


async def test_codex_connector_manifest_policy_and_real_relay_roundtrip(peer, tmp_path):
    local = peer("approval")
    config = AppConfig(Settings(agents=[local.profile]), SecretStore(), None, None)
    launch = ConnectorLaunch("ws://127.0.0.1/connect", "home", tmp_path / "key")
    policies = local_policies(config)
    hello = launch.hello(config)
    assert hello.agents[0].kind == "codex"
    store = Store.open_in(tmp_path / "store")
    store.computers.issue("home", launch.token_file, display_name="Home")
    dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5)
    children = []

    async def factory(alias):
        assert alias == "code"
        transport, _ = await local.open_transport()
        children.append(transport)
        return PolicyTransport(transport, policies[alias])

    remote = AgentClient(
        AgentProfile(
            alias="code",
            kind="codex",
            backend="connector",
            computer_id="home",
            default_cwd=str(tmp_path),
        ),
        pin_dir=tmp_path,
        reconnect_delays=(),
        transport_factory=lambda: dispatcher.open_agent("home", "code"),
    )
    try:
        async with dispatcher.listen("127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"
            connection, welcome = await connect_control(
                url, credential=launch.token_file.read_text().strip(), hello=hello
            )
            relay = RelayPeer(connection, welcome.connection_id, ["code"], local_factory=factory)
            task = asyncio.create_task(relay.run())
            try:
                await relay.ready.wait()
                sid = await remote.new_session()
                assert (await remote.ask(sid, "hello")).text == "Hello world"
                assert remote.supports_cancel
                await remote.close()
                await remote.load_session(sid)
                assert remote.session_availability(sid) == "attached"
                assert sum(f.get("method") == "turn/start" for f in calls(tmp_path)) == 1
            finally:
                await remote.close()
                await connection.close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        for child in children:
            await child.close()
            assert child.process.returncode is not None
        store.close()


@pytest.mark.parametrize("scenario", ["early", "foreign", "stderr", "environment"])
async def test_native_correlation_and_bounded_stderr(peer, tmp_path, monkeypatch, scenario):
    for name in ("ACPGW_GATEWAY__API_TOKEN", "REMOTE_AGENT_SECRET", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(name, "credential-secret")
    client = peer(scenario)
    try:
        sid = await client.new_session()
        assert (await asyncio.wait_for(client.ask(sid, "hello"), 3)).text == "Hello world"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "scenario", ["foreign-permission", "foreign-turn-permission", "grant-root", "unknown"]
)
async def test_foreign_or_unsupported_native_requests_never_reach_approver(
    peer, tmp_path, scenario
):
    client = peer(scenario)

    async def forbidden(request):
        pytest.fail("unowned or persistent approval reached the human")

    client.set_permission_handler(forbidden)
    try:
        sid = await client.new_session()
        assert (await asyncio.wait_for(client.ask(sid, "hello"), 3)).text == "Hello world"
    finally:
        await client.close()
    response = next(f for f in calls(tmp_path) if f.get("id") == "approval-1")
    if scenario == "unknown":
        assert response["error"]["code"] == -32601
    else:
        assert response["result"] == {"decision": "decline"}


async def test_native_withdrawal_revokes_pending_human_authority(peer, tmp_path):
    client = peer("withdraw")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def choose(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.set_permission_handler(choose)
    try:
        sid = await client.new_session()
        assert (await client.ask(sid, "hello")).text == "Hello world"
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(cancelled.wait(), 0.2)
        assert not client._permission_tasks
        assert not client._transport._permissions
    finally:
        await client.close()
    assert not [f for f in calls(tmp_path) if f.get("id") == "approval-1" and "method" not in f]


@pytest.mark.parametrize("scenario", ["disconnect", "malformed", "oversized", "failed", "hold"])
async def test_native_failure_fences_turn_and_reaps_only_owned_child(peer, tmp_path, scenario):
    client = peer(scenario, turn_timeout=0.15)
    try:
        sid = await client.new_session()
        transport = client._transport
        with pytest.raises(AgentError) as exc:
            await asyncio.wait_for(client.ask(sid, "hello"), 2)
        assert "credential-secret" not in str(exc.value)
        assert client.session_availability(sid) == "uncertain"
        before = calls(tmp_path)
        with pytest.raises(SessionNotFound):
            await client.ask(sid, "hello")
        assert calls(tmp_path) == before
    finally:
        await client.close()
    assert transport.process.returncode is not None
    assert not transport._permissions
    assert not transport._requests
    assert not transport._pending
    assert all(task.done() for task in (transport._reader, transport._pump, transport._stderr))


async def test_missing_managed_executable_is_sanitized_without_websocket_fallback(tmp_path):
    profile = AgentProfile(
        alias="code",
        kind="codex",
        backend="codex",
        default_cwd=str(tmp_path),
        codex={"executable": str(tmp_path / "credential-secret-missing")},
    )
    client = AgentClient(profile, pin_dir=tmp_path, reconnect_delays=())
    try:
        with pytest.raises(AgentError) as exc:
            await client.connect()
        assert "credential-secret" not in str(exc.value)
        assert not client.connected
    finally:
        await client.close()


async def test_lost_session_creation_response_closes_managed_stream(peer):
    client = peer("new-timeout", request_timeout=1)
    try:
        await client.connect()
        transport = client._transport
        # Time out creation, not interpreter startup (notably on Windows).
        client._request_timeout = 0.1
        with pytest.raises(AgentError):
            await client.new_session()
        assert not client.connected
        assert transport.process.returncode is not None
    finally:
        await client.close()


async def test_cancelled_session_creation_does_not_leave_mutating_child(peer, tmp_path):
    client = peer("new-timeout")
    try:
        await client.connect()
        transport = client._transport
        task = asyncio.create_task(client.new_session())
        for _ in range(100):
            if any(f.get("method") == "thread/start" for f in calls(tmp_path)):
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.process.returncode is not None
    finally:
        await client.close()


async def test_abandoned_prompt_reaps_child_without_claiming_safe_turn_cancel(peer, tmp_path):
    client = peer("hold", turn_timeout=30)
    try:
        sid = await client.new_session()
        transport = client._transport
        task = asyncio.create_task(client.ask(sid, "hello"))
        for _ in range(100):
            if any(f.get("method") == "turn/start" for f in calls(tmp_path)):
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert transport.process.returncode is not None
        assert client.supports_cancel
        assert client.session_availability(sid) == "uncertain"
    finally:
        await client.close()


async def test_connector_managed_process_activity_and_admission_are_observable(peer, tmp_path):
    import acp_gateway.adapters.codex as adapter
    from acp_gateway.connectors.maintenance import ConnectorMaintenance

    assert hasattr(adapter, "CodexProcessManager"), "connector needs managed process accounting"
    local = peer("hold", turn_timeout=0.3)
    manager = adapter.CodexProcessManager(
        local.profile.codex,
        local_policies(AppConfig(Settings(agents=[local.profile]), SecretStore(), None, None))[
            "code"
        ],
    )
    maintenance = ConnectorMaintenance([local.profile], {"code": manager})
    client = AgentClient(local.profile, pin_dir=tmp_path, transport_factory=manager.open)
    try:
        sid = await client.new_session()
        task = asyncio.create_task(client.ask(sid, "hello"))
        for _ in range(100):
            if manager.maintenance_status()["running_sessions"]:
                break
            await asyncio.sleep(0.005)
        status = maintenance.status()
        assert status["unsupported_agents"] == 0
        assert status["running_sessions"] == 1
        with pytest.raises(ValueError, match="busy"):
            maintenance.apply("prepare")
        with pytest.raises(AgentError):
            await task
        assert maintenance.status()["uncertain_sessions"] == 1
        with pytest.raises(ValueError):
            maintenance.apply("prepare")
    finally:
        await client.close()
        await manager.close()


async def test_connector_reservation_blocks_new_work_on_existing_managed_stream(peer, tmp_path):
    import acp_gateway.adapters.codex as adapter
    from acp_gateway.connectors.maintenance import ConnectorMaintenance

    assert hasattr(adapter, "CodexProcessManager"), "connector needs managed process admission"
    local = peer()
    manager = adapter.CodexProcessManager(
        local.profile.codex,
        local_policies(AppConfig(Settings(agents=[local.profile]), SecretStore(), None, None))[
            "code"
        ],
    )
    maintenance = ConnectorMaintenance([local.profile], {"code": manager})
    client = AgentClient(local.profile, pin_dir=tmp_path, transport_factory=manager.open)
    try:
        sid = await client.new_session()
        assert maintenance.status()["idle_observed"]
        maintenance.apply("prepare")
        with pytest.raises(AgentError):
            await client.ask(sid, "hello")
        assert not [f for f in calls(tmp_path) if f.get("method") == "turn/start"]
    finally:
        await client.close()
        await manager.close()


async def test_native_completed_items_supply_text_once_and_tool_progress(peer):
    from acp_gateway.agents.events import (
        MessageChunk,
        ToolCallStarted,
        ToolCallUpdated,
        TurnFinished,
    )

    client = peer("items")
    try:
        sid = await client.new_session()
        events = [event async for event in client.prompt(sid, "hello")]
        assert "".join(e.text for e in events if isinstance(e, MessageChunk)) == "Hello world"
        assert len([e for e in events if isinstance(e, ToolCallStarted)]) == 1
        assert len([e for e in events if isinstance(e, ToolCallUpdated)]) == 1
        assert isinstance(events[-1], TurnFinished)
    finally:
        await client.close()


async def test_large_native_command_output_keeps_turn_running(peer):
    client = peer("large-output")
    try:
        sid = await client.new_session()
        assert (await client.ask(sid, "hello")).text == "Hello world"
        assert client.session_availability(sid) == "attached"
        assert client._transport.uncertain == 0
    finally:
        await client.close()


def test_live_codex_doctor_uses_owned_runner_and_only_initializes(peer, tmp_path, monkeypatch):
    from acp_gateway.cli import doctor, setup_asyncio

    local = peer()
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps(
            {
                "data_dir": str(tmp_path / "data"),
                "agents": [local.profile.model_dump(mode="json", exclude_none=True)],
            }
        )
    )
    (tmp_path / ".env").write_text("")
    operations, loops = [], []
    original_check = doctor._acp_check

    async def checked(*args):
        loops.append(asyncio.get_running_loop())
        await original_check(*args)

    def owned_run(coro, *, operation):
        operations.append(operation)
        if operation not in setup_asyncio._OPERATIONS:
            coro.close()
            raise ValueError("operation lacks privacy-safe owned-runner diagnostic label")
        return setup_asyncio.run(coro, operation=operation)

    monkeypatch.setattr(doctor, "_acp_check", checked)
    monkeypatch.setattr(doctor, "run_setup", owned_run)
    monkeypatch.setattr(
        doctor.httpx, "get", lambda *args, **kwargs: pytest.fail("unexpected network request")
    )
    checks = doctor.collect(path, live=True, timeout=2)
    row = next(c for c in checks if c["name"] == "agent:code")
    assert row["status"] == "ok", row
    assert operations == ["doctor-codex"]
    assert len(loops) == 1 and loops[0].is_closed()
    assert [f["method"] for f in calls(tmp_path)] == ["initialize", "initialized"]


async def test_command_approval_retains_matching_native_action(peer, tmp_path):
    client = peer("command-retained")
    seen = []

    async def choose(request):
        seen.append(request)
        return request.option("allow_once").option_id

    client.set_permission_handler(choose)
    try:
        sid = await client.new_session()
        assert (await client.ask(sid, "hello")).text == "Hello world"
        assert seen[0].raw_input["command"] == "echo retained"
        assert seen[0].raw_input["cwd"] == str(tmp_path)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "scenario",
    [
        "file-missing",
        "file-empty",
        "file-no-diff",
        "file-other-item",
        "file-other-thread",
        "file-other-turn",
        "file-wrong-type",
        "command-missing",
    ],
)
async def test_approval_without_exact_action_details_is_denied(peer, tmp_path, scenario):
    client = peer(scenario)
    seen = []

    async def choose(request):
        seen.append(request)
        return request.option("allow_once").option_id

    client.set_permission_handler(choose)
    try:
        sid = await client.new_session()
        assert (await client.ask(sid, "hello")).text == "Hello world"
        assert not seen, "incomplete or foreign action must not reach human approval"
    finally:
        await client.close()
    assert next(f for f in calls(tmp_path) if f.get("id") == "approval-1") == {
        "id": "approval-1",
        "result": {"decision": "decline"},
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descendant signalling")
@pytest.mark.parametrize("parent_exits", [False, True])
async def test_shutdown_closes_owned_pipes_without_waiting_for_descendant(peer, parent_exits):
    local = peer()
    policy = local_policies(AppConfig(Settings(agents=[local.profile]), SecretStore(), None, None))[
        "code"
    ]
    manager = CodexProcessManager(local.profile.codex, policy)
    child_code = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdin=subprocess.DEVNULL); "
        "print(child.pid, flush=True); " + ("sys.exit(0)" if parent_exits else "time.sleep(30)")
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    descendant = None
    transport = None
    try:
        descendant = int(await asyncio.wait_for(process.stdout.readline(), 1))
        if parent_exits:
            async with asyncio.timeout(1):
                while process.returncode is None:
                    await asyncio.sleep(0.005)
        transport = CodexAcpTransport(process, local.profile.codex, policy)
        manager._streams.add(transport)
        pipes = [process._transport.get_pipe_transport(fd) for fd in (0, 1, 2)]
        descriptors = [pipe.get_extra_info("pipe") for pipe in pipes]
        await asyncio.wait_for(transport.close(), 3)
        assert all(pipe.is_closing() for pipe in pipes), "owned stdio transports leaked"
        assert all(descriptor.closed for descriptor in descriptors), "owned pipe descriptors leaked"
        assert process._transport._finished, "subprocess transport still awaits descendant EOF"
        assert process._transport.is_closing()
        assert process.returncode is not None
        os.kill(descendant, 0)  # Closing local pipes must not kill the descendant.
        manager.maintenance_status()
        assert not manager._streams
    finally:
        if descendant is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(descendant, signal.SIGKILL)
        process._transport.close()
        if transport is not None:
            await transport.close()
        await asyncio.wait_for(process.wait(), 3)
        await manager.close()
