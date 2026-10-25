"""Adapters dispatch WSL settings through the dedicated owned launcher."""

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp_gateway.agents.client import AgentClient
from acp_gateway.config import AgentProfile


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
@pytest.mark.parametrize("cleanup_confirmed", [False, True])
async def test_managed_adapter_routes_wsl_launch_and_close_without_native_fallback(
    tmp_path, monkeypatch, kind, cleanup_confirmed
):
    adapter = importlib.import_module("acp_gateway.adapters." + kind)
    source = (
        Path(__file__).parents[1]
        / "fakes"
        / ("codex_peer.py" if kind == "codex" else "claude_code_peer.py")
    )
    script = tmp_path / "peer.py"
    script.write_text(source.read_text(), encoding="utf-8")
    calls = []
    closed = []
    processes = []

    async def launch(settings, argv, *, cwd, agent_home_env, limit):
        calls.append((settings, argv, cwd, agent_home_env, limit))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            *argv[1:],
            cwd=tmp_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=limit,
        )
        processes.append(process)
        return process

    async def close(process):
        closed.append(process)
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 3)
        process._transport.close()
        return cleanup_confirmed

    async def forbidden(*args, **kwargs):
        raise AssertionError("native launcher must not run for WSL settings")

    monkeypatch.setitem(
        sys.modules, "acp_gateway.adapters.wsl", SimpleNamespace(spawn_wsl=launch, close_wsl=close)
    )
    monkeypatch.setattr(
        adapter,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": forbidden}),
    )
    profile = AgentProfile(
        alias="code",
        kind=kind,
        backend=kind,
        default_cwd="/home/alice/project",
        **{
            kind: {
                "executable": "/opt/agent",
                "wsl": {"distribution": "Ubuntu-22.04", "user": "alice"},
                "request_timeout": 2,
            }
        },
    )
    client = AgentClient(profile, pin_dir=tmp_path, reconnect_delays=())
    try:
        await client.connect()
        assert client.connected
        client._transport.sessions.create("recorded-id", profile.default_cwd)
        settings, argv, cwd, home, limit = calls[0]
        assert settings.distribution == "Ubuntu-22.04"
        assert settings.user == "alice"
        assert cwd == "/home/alice/project"
        assert argv[0] == "/opt/agent"
        assert home == ("CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR")
        assert limit == 262144
        if kind == "codex":
            assert argv[1:] == ["app-server", "--listen", "stdio://"]
        else:
            assert "--safe-mode" in argv and "--no-session-persistence" not in argv
    finally:
        await client.close()
        for process in processes:
            if process.returncode is None:
                process.kill()
            process._transport.close()
            await process.wait()
    assert closed == processes
    assert all(process.returncode is not None for process in processes)
    from acp_gateway.connectors.policy import LocalPolicyError

    after_restart = client.managed_sessions()
    try:
        if cleanup_confirmed:
            after_restart.load("recorded-id", profile.default_cwd)
        else:
            with pytest.raises(LocalPolicyError, match="unknown"):
                after_restart.load("recorded-id", profile.default_cwd)
    finally:
        after_restart.close()


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
async def test_wsl_launch_failure_surfaces_as_agent_unavailable(tmp_path, monkeypatch, kind):
    from acp_gateway.agents.errors import AgentUnavailable

    importlib.import_module("acp_gateway.adapters." + kind)

    async def refuse(*args, **kwargs):
        raise RuntimeError("WSL transport requires Windows")

    async def close(process):  # pragma: no cover - never reached
        raise AssertionError("nothing was launched")

    monkeypatch.setitem(
        sys.modules, "acp_gateway.adapters.wsl", SimpleNamespace(spawn_wsl=refuse, close_wsl=close)
    )
    profile = AgentProfile(
        alias="code",
        kind=kind,
        backend=kind,
        default_cwd="/home/alice/project",
        **{
            kind: {
                "executable": "/opt/agent",
                "wsl": {"distribution": "Ubuntu-22.04", "user": "alice"},
                "request_timeout": 2,
            }
        },
    )
    client = AgentClient(profile, pin_dir=tmp_path, reconnect_delays=())
    try:
        with pytest.raises(AgentUnavailable):
            await client.connect()
    finally:
        await client.close()
