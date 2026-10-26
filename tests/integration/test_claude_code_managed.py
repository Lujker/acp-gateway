"""Managed Claude wire tests: real portable subprocesses, no credentials/model."""

import asyncio
import contextlib
import importlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp import PROTOCOL_VERSION

from acp_gateway.agents.client import AgentClient
from acp_gateway.agents.errors import AgentUnavailable, SessionNotFound
from acp_gateway.config import AgentProfile, ClaudeCodeSettings
from acp_gateway.connectors.policy import LocalAgentPolicy


@pytest.fixture
async def peer(tmp_path, monkeypatch):
    processes = []
    source = Path(__file__).parents[1] / "fakes" / "claude_code_peer.py"

    async def make(scenario="happy", *, manager=False, sessions=None, **overrides):
        adapter = importlib.import_module("acp_gateway.adapters.claude_code")
        script = tmp_path / "claude-peer.py"
        script.write_text(
            source.read_text().replace('SCENARIO = "happy"', f"SCENARIO = {scenario!r}")
        )
        spawn = asyncio.create_subprocess_exec

        async def launch(executable, *args, **kwargs):
            assert executable == sys.executable
            process = await spawn(executable, str(script), *args, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(
            adapter,
            "asyncio",
            SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": launch}),
        )
        settings = ClaudeCodeSettings(
            **{"executable": sys.executable, "request_timeout": 1, "turn_timeout": 1, **overrides}
        )
        policy = LocalAgentPolicy((str(tmp_path),))
        if manager:
            return adapter.ClaudeCodeProcessManager(settings, policy, sessions=sessions)
        return await adapter.ClaudeCodeAcpTransport.open(settings, policy, sessions=sessions)

    make.processes = processes
    yield make
    # Safety net for failing RED tests; assertions inspect cleanup before here.
    for process in processes:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        process._transport.close()
        await asyncio.wait_for(process.wait(), 3)


async def test_partial_messages_are_correlated_and_not_duplicated(peer, tmp_path):
    stream = await peer("stream")
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        _, frames = await request(
            stream,
            3,
            "session/prompt",
            sessionId=created["result"]["sessionId"],
            prompt=[{"type": "text", "text": "fallback"}],
        )
        assert [
            f["params"]["update"]["content"]["text"]
            for f in frames
            if f.get("method") == "session/update"
        ] == ["streamed reply"]
    finally:
        await stream.close()


def updates(frames):
    return [
        (f["params"]["update"]["sessionUpdate"], f["params"]["update"]["content"]["text"])
        for f in frames
        if f.get("method") == "session/update" and "content" in f["params"]["update"]
    ]


@pytest.mark.parametrize("scenario", ["native-blocks", "native-blocks-plain"])
async def test_per_block_frames_keep_work_updates_and_final_answer(peer, tmp_path, scenario):
    # Claude Code splits one API message into an `assistant` frame per content block
    # (thinking, text, tool_use) with the same message id. Text after thinking and
    # the final answer must survive; text before a tool call is a work update.
    stream = await peer(scenario)
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        response, frames = await request(
            stream,
            3,
            "session/prompt",
            sessionId=created["result"]["sessionId"],
            prompt=[{"type": "text", "text": "go"}],
        )
        assert response["result"]["stopReason"] == "end_turn"
        assert updates(frames) == [
            ("agent_thought_chunk", "Plan."),
            ("agent_thought_chunk", "\n\nChecking."),
            ("agent_message_chunk", "Final answer."),
        ]
        assert "hmm" not in json.dumps(frames)
    finally:
        await stream.close()


async def test_turn_limit_keeps_the_stream_and_reports_latest_progress(peer, tmp_path):
    stream = await peer("native-blocks-max-turns")
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        response, frames = await request(
            stream,
            3,
            "session/prompt",
            sessionId=created["result"]["sessionId"],
            prompt=[{"type": "text", "text": "go"}],
        )
        assert response["result"]["stopReason"] == "max_turn_requests"
        assert updates(frames) == [
            ("agent_thought_chunk", "Plan."),
            ("agent_thought_chunk", "\n\nChecking."),
        ]
        assert not stream.closed.is_set()
        assert stream.uncertain == 0
    finally:
        await stream.close()


async def test_work_profile_enables_native_tools_settings_and_persistence(peer, tmp_path):
    stream = await peer(mode="work")
    await stream.close()
    argv = calls(tmp_path)[0]["frame"]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--tools") + 1] == "default"
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local"
    for flag in (
        "--safe-mode",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--settings",
        "--mcp-config",
    ):
        assert flag not in argv


async def test_native_interrupt_requires_ack_and_terminal_result(peer, tmp_path):
    stream = await peer("interrupt", turn_timeout=3)
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        sid = created["result"]["sessionId"]
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
            }
        )
        for _ in range(100):
            if any(row["frame"].get("type") == "user" for row in calls(tmp_path)):
                break
            await asyncio.sleep(0.01)
        await stream.send(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
        )
        while True:
            frame = await asyncio.wait_for(stream.receive(), 2)
            if frame.get("id") == 3:
                assert frame["result"]["stopReason"] == "cancelled"
                break
        assert stream.uncertain == 0
        assert not stream.closed.is_set()
    finally:
        await stream.close()


@pytest.mark.parametrize("scenario", ["interrupt-no-ack", "interrupt-no-result"])
async def test_incomplete_native_interrupt_never_reports_cancelled(peer, tmp_path, scenario):
    stream = await peer(scenario)
    try:
        await initialize(stream)
        # Keep native process startup separate from the short interrupt deadline.
        stream.settings.request_timeout = 0.15
        stream.settings.turn_timeout = 0.4
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        sid = created["result"]["sessionId"]
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
            }
        )
        for _ in range(100):
            if any(row["frame"].get("type") == "user" for row in calls(tmp_path)):
                break
            await asyncio.sleep(0.005)
        await stream.send(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
        )
        while (frame := await asyncio.wait_for(stream.receive(), 2)) is not None:
            assert frame.get("id") != 3 or "result" not in frame
        assert stream.uncertain >= 1
    finally:
        await stream.close()


@pytest.mark.parametrize("mode,requests", [("safe", 0), ("work", 1)])
async def test_additional_tool_permission_is_forwarded_only_in_work_profile(
    peer, tmp_path, mode, requests
):
    stream = await peer("permission-WebFetch", mode=mode)
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        sid = created["result"]["sessionId"]
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
            }
        )
        seen = []
        while True:
            frame = await asyncio.wait_for(stream.receive(), 2)
            if frame.get("method") == "session/request_permission":
                seen.append(frame)
                await stream.send(
                    {
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": "accept"}},
                    }
                )
            if frame.get("id") == 3:
                break
        assert len(seen) == requests
        decision = next(
            row["frame"]
            for row in calls(tmp_path)
            if row["frame"].get("type") == "control_response"
        )
        assert decision["response"]["response"]["behavior"] == ("allow" if requests else "deny")
    finally:
        await stream.close()


@pytest.mark.parametrize("scenario", ["cancel-permission", "result-permission"])
async def test_native_revocation_withdraws_once_only_human_authority(peer, tmp_path, scenario):
    stream = await peer(scenario)
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        sid = created["result"]["sessionId"]
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hello"}]},
            }
        )
        permission = await asyncio.wait_for(stream.receive(), 2)
        assert permission["method"] == "session/request_permission"
        withdrawn = await asyncio.wait_for(stream.receive(), 2)
        update = withdrawn["params"]["update"]
        assert update["toolCallId"] == permission["id"]
        assert update["_meta"]["acp-gateway"]["permissionWithdrawn"] is True
        assert not stream._permissions
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": permission["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "accept"}},
            }
        )
        while (frame := await asyncio.wait_for(stream.receive(), 2)).get("id") != 3:
            pass
        assert frame["result"]["stopReason"] == "end_turn"
        assert not any(f["frame"].get("type") == "control_response" for f in calls(tmp_path))
    finally:
        await stream.close()


async def test_manager_exposes_busy_and_uncertain_maintenance_admission(peer, tmp_path):
    manager = await peer("hang", manager=True)
    stream = await manager.open()
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        sid = created["result"]["sessionId"]
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hello"}]},
            }
        )
        for _ in range(100):
            if manager.maintenance_status()["running_sessions"]:
                break
            await asyncio.sleep(0.01)
        assert manager.maintenance_status() == {
            "pending_requests": 1,
            "pending_approvals": 0,
            "running_sessions": 1,
            "uncertain_sessions": 0,
            "opening": 0,
        }
        manager.maintenance_reserved = True
        with pytest.raises(AgentUnavailable, match="maintenance"):
            await manager.open()
        await stream.close()
        assert manager.maintenance_status()["uncertain_sessions"] == 1
        assert manager.maintenance_status()["running_sessions"] == 0
    finally:
        await manager.close()


@pytest.mark.parametrize("concurrent", [False, True])
async def test_manager_process_capacity_is_global_and_race_safe(peer, tmp_path, concurrent):
    manager = await peer(manager=True, max_sessions=2)
    streams = []
    try:
        if concurrent:
            outcomes = await asyncio.gather(
                *(manager.open() for _ in range(3)), return_exceptions=True
            )
            streams = [s for s in outcomes if not isinstance(s, BaseException)]
            assert len(streams) == 2
            assert sum(isinstance(s, AgentUnavailable) for s in outcomes) == 1
        else:
            streams = [await manager.open(), await manager.open()]
            with pytest.raises(AgentUnavailable, match="capacity"):
                await manager.open()
            stream = streams[0]
            await initialize(stream)
            await request(stream, 2, "session/new", cwd=str(tmp_path))
            await stream.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/new",
                    "params": {"cwd": str(tmp_path)},
                }
            )
            assert await asyncio.wait_for(stream.receive(), 3) is None
        assert len({process.pid for process in peer.processes}) == 2
    finally:
        await manager.close()
    assert all(c["process"].returncode is not None for s in streams for c in s._children.values())


async def test_binding_loss_counts_each_interrupted_conversation(peer, tmp_path):
    manager = await peer("hang", manager=True)
    stream = await manager.open()
    try:
        await initialize(stream)
        for rid in (2, 3):
            created, _ = await request(stream, rid, "session/new", cwd=str(tmp_path))
            await stream.send(
                {
                    "jsonrpc": "2.0",
                    "id": rid + 10,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": created["result"]["sessionId"],
                        "prompt": [{"type": "text", "text": "hello"}],
                    },
                }
            )
        for _ in range(100):
            if len(stream._turns) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(stream._turns) == 2
        await stream.close()
        assert manager.maintenance_status()["uncertain_sessions"] == 2
    finally:
        await manager.close()


async def test_manager_close_fences_inflight_initialization(peer, tmp_path):
    manager = await peer("slow-init", manager=True)
    opening = asyncio.create_task(manager.open())
    for _ in range(100):
        if manager.maintenance_status()["opening"]:
            break
        await asyncio.sleep(0.001)
    await manager.close()
    outcome = (await asyncio.gather(opening, return_exceptions=True))[0]
    assert isinstance(outcome, AgentUnavailable)
    assert not manager._streams
    assert manager.maintenance_status()["opening"] == 0


async def test_duplicate_native_permission_identity_fails_closed(peer, tmp_path):
    stream = await peer("duplicate-permission")
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": created["result"]["sessionId"],
                    "prompt": [{"type": "text", "text": "hello"}],
                },
            }
        )
        await asyncio.wait_for(stream.closed.wait(), 2)
        await stream.close()
        assert not stream._permissions
        assert stream.uncertain == 1
        assert not any(f["frame"].get("type") == "control_response" for f in calls(tmp_path))
    finally:
        await stream.close()


@pytest.mark.parametrize(
    "scenario",
    [
        "error-result",
        "error-subtype",
        "disconnect",
        "hang",
        "malformed",
        "oversize",
        "truncated",
        "foreign-result",
        "flood",
    ],
)
async def test_failure_is_sanitized_bounded_and_never_replayed(peer, tmp_path, caplog, scenario):
    stream = await peer(scenario, turn_timeout=0.15)
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": created["result"]["sessionId"],
                    "prompt": [{"type": "text", "text": "hello"}],
                },
            }
        )
        await asyncio.wait_for(stream.closed.wait(), 2)
        await stream.close()
        frames = []
        while (frame := await stream.receive()) is not None:
            frames.append(frame)
        assert not any(f.get("id") == 3 and "result" in f for f in frames)
        assert stream.uncertain == 1
        with pytest.raises(ConnectionError):
            await stream.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/new",
                    "params": {"cwd": str(tmp_path)},
                }
            )
        assert len([f for f in calls(tmp_path) if f["frame"].get("type") == "user"]) == 1
        assert "native-secret" not in caplog.text
        assert all(c["process"].returncode is not None for c in stream._children.values())
    finally:
        await stream.close()


@pytest.mark.parametrize("scenario", ["bad-init", "hang-init", "eof-init"])
async def test_failed_initialization_reaps_child_without_prompt(peer, tmp_path, scenario):
    with pytest.raises(AgentUnavailable, match="initialization"):
        await peer(scenario)
    assert all(p.returncode is not None and p._transport.is_closing() for p in peer.processes)
    assert not any(f["frame"].get("type") == "user" for f in calls(tmp_path))


async def test_native_stderr_and_environment_never_disclose_gateway_secrets(
    peer, tmp_path, monkeypatch, caplog
):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "ACPGW_GATEWAY__API_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_BASH_ENV_FILE",
        "REMOTE_AGENT_SECRET",
    ):
        monkeypatch.setenv(key, "gateway-secret")
    stream = await peer("environment")
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        _, frames = await request(
            stream,
            3,
            "session/prompt",
            sessionId=created["result"]["sessionId"],
            prompt=[{"type": "text", "text": "hello"}],
        )
        assert "native-secret" not in json.dumps(frames)
        assert "native-secret" not in caplog.text
        assert "gateway-secret" not in caplog.text
    finally:
        await stream.close()


async def test_unknown_native_control_has_typed_error_not_human_approval(peer, tmp_path):
    stream = await peer("unknown-control")
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        _, frames = await request(
            stream,
            3,
            "session/prompt",
            sessionId=created["result"]["sessionId"],
            prompt=[{"type": "text", "text": "hello"}],
        )
        assert all(f.get("method") != "session/request_permission" for f in frames)
        reply = next(
            f["frame"] for f in calls(tmp_path) if f["frame"].get("type") == "control_response"
        )
        assert reply["response"]["subtype"] == "error"
    finally:
        await stream.close()


@pytest.mark.parametrize("allow", [False, True])
async def test_real_agent_client_permission_roundtrip_and_lost_session_fence(peer, tmp_path, allow):
    stream = await peer("permission-Edit")

    async def factory():
        return stream

    client = AgentClient(
        AgentProfile(
            alias="code",
            kind="claude_code",
            backend="claude_code",
            default_cwd=str(tmp_path),
            claude_code=stream.settings,
        ),
        pin_dir=tmp_path,
        request_timeout=2,
        reconnect_delays=(),
        transport_factory=factory,
    )
    seen = []

    async def choose(permission):
        seen.append(permission)
        assert permission.raw_input["input"]["old_string"] == "old"
        assert permission.raw_input["input"]["new_string"] == "new"
        return permission.option("allow_once").option_id

    if allow:
        client.set_permission_handler(choose)
    try:
        sid = await client.new_session()
        reply = await asyncio.wait_for(client.ask(sid, "hello"), 3)
        assert reply.text == "hello"
        assert reply.stop_reason == "end_turn"
        assert bool(seen) == allow
        assert client.supports_cancel
        assert not client.agent_capabilities.load_session
        assert [s["sessionId"] for s in await client.list_sessions()] == [sid]
    finally:
        await client.close()
    before = calls(tmp_path)
    with pytest.raises(SessionNotFound):
        await client.ask(sid, "must not replay")
    with pytest.raises(SessionNotFound):
        await client.load_session(sid)
    assert calls(tmp_path) == before


@pytest.mark.parametrize("exited", [False, True])
async def test_close_closes_local_pipes_despite_inheriting_descendant(peer, tmp_path, exited):
    stream = await peer("descendant-exited" if exited else "descendant-live")
    descendant = None
    try:
        for _ in range(200):
            descendant = next(
                (
                    f["frame"].get("descendant")
                    for f in calls(tmp_path)
                    if "descendant" in f["frame"]
                ),
                None,
            )
            if descendant:
                break
            await asyncio.sleep(0.01)
        assert descendant
        process = stream.process
        transport = process._transport
        pipes = [transport.get_pipe_transport(fd) for fd in (0, 1, 2)]
        descriptors = [p.get_extra_info("pipe") for p in pipes]
        await asyncio.wait_for(stream.close(), 3)
        assert process.returncode is not None
        assert transport.is_closing()
        assert all(p.is_closing() for p in pipes)
        for descriptor in descriptors:
            with pytest.raises(ValueError):
                descriptor.fileno()
        assert await asyncio.wait_for(transport._wait(), 0.5) is not None
        if sys.platform == "win32":
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {descendant}", "/FO", "CSV", "/NH"],
                capture_output=True,
                timeout=5,
            )
            assert f'"{descendant}"'.encode("ascii") in listing.stdout
        else:
            os.kill(descendant, 0)
    finally:
        await stream.close()
        if descendant is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(descendant, signal.SIGTERM)


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "claude-peer.log").read_text().splitlines()]


async def request(stream, rid, method, **params):
    await stream.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    frames = []
    while True:
        frame = await asyncio.wait_for(stream.receive(), 3)
        assert frame is not None, frames
        frames.append(frame)
        if frame.get("id") == rid:
            return frame, frames


async def initialize(stream):
    return await request(stream, 1, "initialize", protocolVersion=PROTOCOL_VERSION)


async def test_initialize_is_model_free_manual_and_closes_owned_child(peer, tmp_path):
    stream = await peer()
    try:
        response, _ = await initialize(stream)
        caps = response["result"]["agentCapabilities"]
        assert not caps["loadSession"]
        assert caps["_meta"]["acp-gateway"]["cancel"]
        assert not caps["_meta"]["acp-gateway"]["coldResume"]
        recorded = calls(tmp_path)
        assert [f["frame"].get("type") for f in recorded] == [None, "control_request"]
        argv = recorded[0]["frame"]["argv"]
        assert argv[argv.index("--permission-mode") + 1] == "manual"
        for flag in (
            "--strict-mcp-config",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-chrome",
        ):
            assert flag in argv
        assert "--no-session-persistence" not in argv
        assert "--dangerously-skip-permissions" not in argv
        configured = json.loads(argv[argv.index("--settings") + 1])
        assert configured == {
            "disableAllHooks": True,
            "permissions": {"ask": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]},
        }
        assert "--allowedTools" not in argv
        assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,Bash"
        child = stream.process
    finally:
        await stream.close()
    assert child.returncode is not None
    assert child._transport.is_closing()


async def test_separate_process_conversations_keep_sequential_history(peer, tmp_path):
    stream = await peer()
    try:
        await initialize(stream)
        first, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        second, _ = await request(stream, 3, "session/new", cwd=str(tmp_path))
        a, b = first["result"]["sessionId"], second["result"]["sessionId"]
        assert a != b
        for rid, sid, text, expected in [
            (4, a, "one", "one"),
            (5, b, "two", "two"),
            (6, a, "three", "one|three"),
        ]:
            result, frames = await request(
                stream,
                rid,
                "session/prompt",
                sessionId=sid,
                prompt=[{"type": "text", "text": text}],
            )
            assert result["result"] == {"stopReason": "end_turn"}
            assert (
                "".join(
                    f.get("params", {}).get("update", {}).get("content", {}).get("text", "")
                    for f in frames
                )
                == expected
            )
        listing, _ = await request(stream, 7, "session/list", cwd=str(tmp_path))
        assert {s["sessionId"] for s in listing["result"]["sessions"]} == {a, b}
        recorded = calls(tmp_path)
        assert len({f["pid"] for f in recorded}) == 2
        users = [f for f in recorded if f["frame"].get("type") == "user"]
        assert [f["sid"] for f in users] == [a, b, a]
        assert all(f["frame"]["session_id"] == f["sid"] for f in users)
        processes = [c["process"] for c in stream._children.values()]
    finally:
        await stream.close()
    assert all(p.returncode is not None for p in processes)


async def test_empty_claude_can_reconnect_then_resume_real_history(peer, tmp_path):
    from acp_gateway.adapters.managed_sessions import ManagedSessions

    profile = AgentProfile(
        alias="claude",
        kind="claude_code",
        backend="claude_code",
        default_cwd=str(tmp_path),
        claude_code={"executable": sys.executable},
    )

    def bindings():
        return ManagedSessions(tmp_path / "bindings", profile)

    first = await peer(sessions=bindings())
    try:
        await initialize(first)
        response, _ = await request(first, 2, "session/new", cwd=str(tmp_path))
        sid = response["result"]["sessionId"]
    finally:
        await first.close()
    assert not (tmp_path / ("claude-history-" + sid + ".json")).exists()
    second = await peer(sessions=bindings())
    try:
        await initialize(second)
        await request(second, 2, "session/load", sessionId=sid, cwd=str(tmp_path))
        argv = [f["frame"]["argv"] for f in calls(tmp_path) if "argv" in f["frame"]][-1]
        assert "--resume" not in argv and f"--session-id={sid}" in argv
        assert not [f for f in calls(tmp_path) if f["frame"].get("type") == "user"]
        response, _ = await request(
            second, 3, "session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "first"}]
        )
        assert response["result"]["stopReason"] == "end_turn"
    finally:
        await second.close()
    third = await peer(sessions=bindings())
    try:
        await initialize(third)
        await request(third, 2, "session/load", sessionId=sid, cwd=str(tmp_path))
        argv = [f["frame"]["argv"] for f in calls(tmp_path) if "argv" in f["frame"]][-1]
        assert argv[argv.index("--resume") + 1] == sid
        response, frames = await request(
            third, 3, "session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "second"}]
        )
        assert response["result"]["stopReason"] == "end_turn"
        text = "".join(
            f.get("params", {}).get("update", {}).get("content", {}).get("text", "") for f in frames
        )
        assert text == "first|second"
    finally:
        await third.close()


async def test_legacy_missing_claude_history_returns_error_without_fresh_session(peer, tmp_path):
    from uuid import uuid4

    from acp_gateway.adapters.managed_sessions import ManagedSessions

    profile = AgentProfile(
        alias="claude",
        kind="claude_code",
        backend="claude_code",
        default_cwd=str(tmp_path),
        claude_code={"executable": sys.executable},
    )
    storage = ManagedSessions(tmp_path / "bindings", profile)
    sid = str(uuid4())
    storage.create(sid, str(tmp_path))
    storage.close()
    stream = await peer(sessions=ManagedSessions(tmp_path / "bindings", profile))
    try:
        await initialize(stream)
        response, _ = await request(stream, 2, "session/load", sessionId=sid, cwd=str(tmp_path))
        assert response["error"]["code"] == -32001
        assert "history" in response["error"]["message"]
        argv = [f["frame"]["argv"] for f in calls(tmp_path) if "argv" in f["frame"]]
        assert not any(f"--session-id={sid}" in args for args in argv)
        assert not [f for f in calls(tmp_path) if f["frame"].get("type") == "user"]
    finally:
        await stream.close()


@pytest.mark.parametrize(
    "tool,input_data",
    [
        ("Bash", {"command": "echo reviewed", "description": "test command"}),
        ("Edit", {"file_path": "/review.txt", "old_string": "old", "new_string": "new"}),
        ("Write", {"file_path": "/review.txt", "content": "reviewed"}),
        ("Read", {"file_path": "/review.txt"}),
        ("Glob", {"pattern": "*.py"}),
        ("Grep", {"pattern": "reviewed"}),
    ],
)
@pytest.mark.parametrize("allow", [False, True])
async def test_permissions_are_explicit_once_original_input(
    peer, tmp_path, tool, input_data, allow
):
    stream = await peer("permission-" + tool)
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        sid = created["result"]["sessionId"]
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hello"}]},
            }
        )
        permission = await asyncio.wait_for(stream.receive(), 2)
        assert permission["method"] == "session/request_permission"
        details = permission["params"]["toolCall"]["rawInput"]
        assert details["input"] == input_data
        assert details["tool_name"] == tool
        assert (
            permission["params"]["toolCall"]["kind"]
            == {
                "Bash": "execute",
                "Edit": "edit",
                "Write": "edit",
                "Read": "read",
                "Glob": "search",
                "Grep": "search",
            }[tool]
        )
        assert details["cwd"] == str(tmp_path)
        assert {o["kind"] for o in permission["params"]["options"]} == {"allow_once", "reject_once"}
        await stream.send(
            {
                "jsonrpc": "2.0",
                "id": permission["id"],
                "result": {
                    "outcome": {"outcome": "selected", "optionId": "accept" if allow else "decline"}
                },
            }
        )
        while (frame := await asyncio.wait_for(stream.receive(), 2)).get("id") != 3:
            pass
        assert frame["result"]["stopReason"] == "end_turn"
        reply = next(
            f["frame"] for f in calls(tmp_path) if f["frame"].get("type") == "control_response"
        )
        expected = (
            {"behavior": "allow", "updatedInput": input_data}
            if allow
            else {"behavior": "deny", "message": "Denied by gateway"}
        )
        assert reply["response"]["response"] == expected
    finally:
        await stream.close()


@pytest.mark.parametrize(
    "scenario",
    ["missing", "unknown", "agent", "foreign", "empty-command", "incomplete-edit", "background"],
)
async def test_unsafe_permission_is_denied_without_human_authority(peer, tmp_path, scenario):
    stream = await peer("unsafe-" + scenario)
    try:
        await initialize(stream)
        created, _ = await request(stream, 2, "session/new", cwd=str(tmp_path))
        result, frames = await request(
            stream,
            3,
            "session/prompt",
            sessionId=created["result"]["sessionId"],
            prompt=[{"type": "text", "text": "hello"}],
        )
        assert result["result"]["stopReason"] == "end_turn"
        assert all(f.get("method") != "session/request_permission" for f in frames)
        reply = next(
            f["frame"] for f in calls(tmp_path) if f["frame"].get("type") == "control_response"
        )
        assert reply["response"]["response"]["behavior"] == "deny"
    finally:
        await stream.close()


async def controlled_native_turn(peer, monkeypatch):
    """Drive the real reader with deterministic frames and observe actual writes."""
    stream = await peer("hang")
    child = stream._warm
    child["reader"].cancel()
    await asyncio.gather(child["reader"], return_exceptions=True)
    sid = child["sid"]
    turn = stream._new_turn(asyncio.get_running_loop().create_future())
    stream._sessions[sid] = child
    stream._turns[sid] = turn
    frames = asyncio.Queue()
    previous = False

    async def next_frame(owner):
        nonlocal previous
        assert owner is child
        if previous:
            frames.task_done()
        frame = await frames.get()
        previous = True
        return frame

    monkeypatch.setattr(stream, "_frame", next_frame)
    writes = []
    write = child["process"].stdin.write

    def record(data):
        writes.append(data)
        write(data)

    monkeypatch.setattr(child["process"].stdin, "write", record)
    child["reader"] = asyncio.create_task(stream._read(child))
    return stream, child, turn, frames, writes


@pytest.mark.parametrize("streamed", [False, True])
async def test_error_marked_assistant_never_publishes_diagnostics(
    peer, monkeypatch, caplog, streamed
):
    stream, child, turn, frames, _ = await controlled_native_turn(peer, monkeypatch)
    sid = child["sid"]
    try:
        if streamed:
            for event in (
                {"type": "message_start", "message": {"id": "prior"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "safe"}},
            ):
                frames.put_nowait({"type": "stream_event", "session_id": sid, "event": event})
            await asyncio.wait_for(frames.join(), 2)
        frames.put_nowait(
            {
                "type": "assistant",
                "session_id": sid,
                "error": "authentication_failed",
                "message": {
                    "id": "error-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "API Error: native-private-diagnostic"}],
                },
            }
        )
        frames.put_nowait(
            {
                "type": "result",
                "session_id": sid,
                "subtype": "error_during_execution",
                "is_error": True,
            }
        )
        await asyncio.wait_for(stream.closed.wait(), 2)
        await stream.close()
        published = []
        while (frame := await stream.receive()) is not None:
            published.append(frame)
        assert "native-private-diagnostic" not in json.dumps(published)
        assert "authentication_failed" not in json.dumps(published)
        assert "native-private-diagnostic" not in caplog.text
        assert "authentication_failed" not in caplog.text
        assert "native-private-diagnostic" not in repr(child["reader"].exception())
        assert not turn["done"].done()
        assert stream.uncertain == 1
        # Text of an unfinished native message is held, so a failed turn publishes none.
        assert published == []
    finally:
        await stream.close()


@pytest.mark.parametrize("choice", ["accept", "decline"])
@pytest.mark.parametrize("revocation", ["cancel", "result", "close"])
async def test_queued_permission_decision_remains_revocable(peer, monkeypatch, choice, revocation):
    stream, child, turn, frames, writes = await controlled_native_turn(peer, monkeypatch)
    sid = child["sid"]
    blocked = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def acquire(self):
            if self.locked():
                blocked.set()
            return await super().acquire()

    lock = child["lock"] = ObservedLock()
    decision = None
    try:
        frames.put_nowait(
            {
                "type": "control_request",
                "request_id": "native-approval",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "tool_use_id": "tool-1",
                    "input": {"command": "echo reviewed"},
                },
            }
        )
        permission = await asyncio.wait_for(stream.receive(), 2)
        assert permission["method"] == "session/request_permission"
        await lock.acquire()
        decision = asyncio.create_task(
            stream.send(
                {
                    "id": permission["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": choice}},
                }
            )
        )
        await asyncio.wait_for(blocked.wait(), 2)
        if revocation == "close":
            await stream.close()
        else:
            frames.put_nowait(
                {"type": "control_cancel_request", "request_id": "native-approval"}
                if revocation == "cancel"
                else {"type": "result", "session_id": sid, "subtype": "success", "is_error": False}
            )
            await asyncio.wait_for(frames.join(), 2)
        lock.release()
        outcomes = await asyncio.wait_for(asyncio.gather(decision, return_exceptions=True), 2)
        assert writes == [], "a revoked queued decision must never reach native stdin"
        assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
        assert not stream._permissions
        if revocation != "close":
            withdrawn = await asyncio.wait_for(stream.receive(), 2)
            assert withdrawn["params"]["update"] == {
                "sessionUpdate": "tool_call_update",
                "toolCallId": permission["id"],
                "status": "failed",
                "_meta": {"acp-gateway": {"permissionWithdrawn": True}},
            }
            assert stream._out.empty()
        assert turn["done"].done() == (revocation == "result")
    finally:
        if lock.locked():
            lock.release()
        if decision is not None:
            await asyncio.gather(decision, return_exceptions=True)
        await stream.close()
