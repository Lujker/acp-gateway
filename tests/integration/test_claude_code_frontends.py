"""Real frontend paths over a synthetic Claude peer; no model/account access."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp_gateway.agents.client import AgentClient
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.launch import ConnectorLaunch, local_policies
from acp_gateway.connectors.policy import PolicyTransport
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.storage import Store


@pytest.fixture
def native_profile(tmp_path, monkeypatch):
    import acp_gateway.adapters.claude_code as adapter

    source = Path(__file__).parents[1] / "fakes" / "claude_code_peer.py"
    script = tmp_path / "peer.py"
    script.write_text(source.read_text())
    spawn = asyncio.create_subprocess_exec
    children = []

    async def launch(executable, *args, **kwargs):
        assert executable == sys.executable
        child = await spawn(executable, str(script), *args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(
        adapter, "asyncio", SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": launch})
    )
    profile = AgentProfile(
        alias="claude",
        kind="claude_code",
        backend="claude_code",
        default_cwd=str(tmp_path),
        claude_code={"executable": sys.executable, "request_timeout": 2, "turn_timeout": 2},
    )
    yield profile, children
    assert children and all(child.returncode is not None for child in children)


async def test_claude_real_agent_client_resumes_original_history_after_restart(
    native_profile, tmp_path
):
    profile, children = native_profile
    client = AgentClient(profile, pin_dir=tmp_path, reconnect_delays=())
    try:
        sid = await client.new_session()
        assert client.supports_cancel
        assert client.agent_capabilities.load_session
        assert (await client.ask(sid, "first")).text == "first"
        assert (await client.ask(sid, "second")).text == "first|second"
        rows = await client.list_sessions()
        assert [row["sessionId"] for row in rows] == [sid]
        await client.close()
        count = len(children)
        client = AgentClient(profile, pin_dir=tmp_path, reconnect_delays=())
        await client.load_session(sid)
        assert (await client.ask(sid, "third")).text == "first|second|third"
        assert len(children) > count
    finally:
        await client.close()
    frames = [json.loads(line) for line in (tmp_path / "claude-peer.log").read_text().splitlines()]
    assert all(frame["sid"] == sid for frame in frames if frame["frame"].get("type") == "user")
    assert [
        frame["frame"]["message"]["content"]
        for frame in frames
        if frame["frame"].get("type") == "user"
    ] == ["first", "second", "third"]


async def test_claude_real_relay_carries_manifest_owned_sessions_and_caps(native_profile, tmp_path):
    profile, _ = native_profile
    cfg = AppConfig(Settings(agents=[profile]), SecretStore(), None, None)
    launch = ConnectorLaunch("ws://127.0.0.1/connect", "pc", tmp_path / "key")
    policy = local_policies(cfg)["claude"]
    store = Store.open_in(tmp_path / "store")
    store.computers.issue("pc", launch.token_file, display_name="PC")
    dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5)
    local = AgentClient(profile, pin_dir=tmp_path)
    streams = []

    async def factory(alias):
        assert alias == "claude"
        transport, _ = await local.open_transport()
        streams.append(transport)
        return PolicyTransport(transport, policy)

    remote = AgentClient(
        AgentProfile(
            alias="claude",
            kind="claude_code",
            backend="connector",
            computer_id="pc",
            default_cwd=str(tmp_path),
        ),
        pin_dir=tmp_path,
        reconnect_delays=(),
        transport_factory=lambda: dispatcher.open_agent("pc", "claude"),
    )
    try:
        async with dispatcher.listen("127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"
            connection, welcome = await connect_control(
                url, credential=launch.token_file.read_text().strip(), hello=launch.hello(cfg)
            )
            relay = RelayPeer(connection, welcome.connection_id, ["claude"], local_factory=factory)
            task = asyncio.create_task(relay.run())
            try:
                await relay.ready.wait()
                sid = await remote.new_session()
                assert (await remote.ask(sid, "through relay")).text == "through relay"
                assert remote.supports_cancel
                await remote.close()
                await remote.load_session(sid)
                assert remote.session_availability(sid) == "attached"
            finally:
                await remote.close()
                await connection.close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        for stream in streams:
            await stream.close()
        store.close()


def test_claude_live_doctor_real_peer_only_initializes_owned_child(
    native_profile, tmp_path, monkeypatch
):
    from acp_gateway.cli import doctor, setup_asyncio

    profile, _ = native_profile
    cfg = AppConfig(Settings(agents=[profile]), SecretStore(), None, None)
    path = tmp_path / "config.yaml"
    path.write_text(cfg.settings.model_dump_json(exclude_none=True))
    (tmp_path / ".env").write_text("")
    operations = []

    def run(coro, *, operation):
        operations.append(operation)
        return setup_asyncio.run(coro, operation=operation)

    monkeypatch.setattr(doctor, "run_setup", run)
    checks = doctor.collect(path, live=True, timeout=3)
    row = next(row for row in checks if row["name"] == "agent:claude")
    assert row["status"] == "ok", row
    assert operations == ["doctor-claude-code"]
    frames = [
        json.loads(line)["frame"]
        for line in (tmp_path / "claude-peer.log").read_text().splitlines()
    ]
    assert [frame["type"] for frame in frames if "type" in frame] == ["control_request"]
    assert frames[-1]["request"]["subtype"] == "initialize"
