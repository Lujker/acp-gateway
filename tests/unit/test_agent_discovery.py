"""Local discovery owns execution identity; wire inputs cannot supply it."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from acp_gateway.connectors.protocol import (
    ProfileSpec,
    ProfilesRequest,
    ProtocolError,
    decode_frame,
)


@pytest.mark.parametrize("field", ["executable", "argv", "user", "wsl", "env"])
def test_remote_candidate_spec_cannot_select_execution_identity(field):
    req = ProfilesRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        actor="telegram:1",
        expires=time.time() + 30,
        action="add",
        profile=ProfileSpec(
            alias="code", candidate="a" * 32, title="Code", cwd="/work", mode="safe"
        ),
    ).model_dump(mode="json", exclude_none=True)
    req["profile"][field] = "/untrusted"
    with pytest.raises(ProtocolError):
        decode_frame(json.dumps(req))


def record():
    return {
        "home": "/home/alice",
        "user": "alice",
        "python": "/usr/bin/python3",
        "agents": [
            {"kind": "codex", "executable": "/opt/cli/codex", "path": ["/opt/cli", "/usr/bin"]}
        ],
    }


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
async def test_native_candidates_keep_paths_local_and_ids_expire(monkeypatch, platform):
    from acp_gateway.connectors import discovery as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(module._discovery_probe, "inventory", record)
    monkeypatch.delenv("SystemRoot", raising=False)
    output = AsyncMock(return_value=b"codex-cli 1.2.3\n")
    monkeypatch.setattr(module, "output", output)
    discovery = module.AgentDiscovery()
    public, hints = await discovery.discover()
    assert len(public) == 1
    candidate = public[0]
    assert "1.2.3" in candidate.title
    assert {"linux": "Linux", "darwin": "macOS", "win32": "Windows"}[platform] in candidate.title
    assert "/opt/cli" not in candidate.model_dump_json()
    assert discovery.resolve(candidate.id).codex.executable == "/opt/cli/codex"
    assert any("Claude Code" in hint for hint in hints)
    output.assert_awaited_once_with(["/opt/cli/codex", "--version"])
    await discovery.discover()
    with pytest.raises(ValueError):
        discovery.resolve(candidate.id)
    discovery.expires = time.monotonic() - 1
    with pytest.raises(ValueError):
        discovery.resolve("a" * 32)


async def test_wsl2_discovery_pins_default_account_python_and_path(monkeypatch):
    from acp_gateway.connectors import discovery as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setattr(module._discovery_probe, "inventory", lambda: {**record(), "agents": []})
    calls = []

    async def output(argv, **kwargs):
        calls.append(argv)
        if "--list" in argv:
            return (
                "  NAME              STATE     VERSION\n"
                "* Ubuntu-22.04      Running   2\n"
                "  Old Linux         Stopped   1\n"
            ).encode("utf-16-le")
        if "/bin/sh" in argv:
            # A login shell profile may print before the probe's JSON line.
            return b"Welcome to Ubuntu\n" + json.dumps(record()).encode() + b"\n"
        return b"codex-cli 1.2.3\n"

    monkeypatch.setattr(module, "output", output)
    discovery = module.AgentDiscovery()
    public, _ = await discovery.discover()
    assert len(public) == 1
    profile = discovery.resolve(public[0].id)
    assert profile.codex.wsl.distribution == "Ubuntu-22.04"
    assert profile.codex.wsl.user == "alice"
    assert profile.codex.wsl.python == "/usr/bin/python3"
    assert profile.codex.wsl.path == ["/opt/cli", "/usr/bin"]
    assert "Old Linux" not in str(calls)
    assert all(argv[0] == str(Path("C:/Windows") / "System32/wsl.exe") for argv in calls)
    assert calls[-1][-2:] == ["/opt/cli/codex", "--version"]
    assert "--user" in calls[-1] and "alice" in calls[-1]


async def test_broken_cli_is_a_hint_without_private_error(monkeypatch):
    from acp_gateway.connectors import discovery as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module._discovery_probe, "inventory", record)
    monkeypatch.setattr(module, "output", AsyncMock(side_effect=OSError("PRIVATE")))
    public, hints = await module.AgentDiscovery().discover()
    assert not public
    assert len(hints) == 2 and "PRIVATE" not in str(hints)


async def test_subprocess_output_is_complete_bounded_and_timed():
    from acp_gateway.connectors.discovery import output

    data = await output([sys.executable, "-c", "print('x' * 8000)"])
    assert data.strip() == b"x" * 8000
    with pytest.raises(ValueError, match="too large"):
        await output([sys.executable, "-c", "print('x' * 20000)"])
    with pytest.raises(TimeoutError):
        await output([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


@pytest.mark.skipif(os.name == "nt", reason="POSIX CLI install locations")
def test_native_inventory_uses_known_install_paths(tmp_path, monkeypatch):
    from acp_gateway.connectors import _discovery_probe as module

    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        module, "sys", SimpleNamespace(platform="linux", executable="/usr/bin/python3")
    )
    binary = tmp_path / ".nvm/versions/node/v22/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("fake, never launched")
    binary.chmod(0o700)
    rows = module.inventory()["agents"]
    assert any(row["executable"] == str(binary) and row["kind"] == "codex" for row in rows)


def test_windows_npm_codex_resolves_native_binary(tmp_path, monkeypatch):
    from acp_gateway.connectors import _discovery_probe as module

    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32", executable="python.exe"))
    npm = tmp_path / "npm"
    shim = npm / "codex.cmd"
    binary = (
        npm
        / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
        / "vendor/x86_64-pc-windows-msvc/codex/codex.exe"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text("fake, never launched")
    binary.chmod(0o700)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(module.shutil, "which", lambda name: str(shim) if name == "codex" else None)
    assert module.inventory()["agents"][0]["executable"] == str(binary)


async def test_missing_hints_only_for_agents_found_nowhere(monkeypatch):
    from acp_gateway.connectors import discovery as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    native = {**record(), "agents": [], "shims": ["claude_code"]}
    monkeypatch.setattr(module._discovery_probe, "inventory", lambda: native)

    async def output(argv, **kwargs):
        if "--list" in argv:
            return "* Ubuntu   Running   2\n".encode("utf-16-le")
        if "/bin/sh" in argv:
            return json.dumps(record()).encode()
        return b"codex-cli 1.2.3\n"

    monkeypatch.setattr(module, "output", output)
    public, hints = await module.AgentDiscovery().discover()
    assert [c.kind for c in public] == ["codex"]
    # Codex exists in WSL, so Windows does not report it missing.
    assert not any("Codex" in hint for hint in hints)
    assert any(h.startswith("Windows: Claude Code") and "claude install" in h for h in hints)
    assert any(h.startswith("Ubuntu / alice: Claude Code") for h in hints)


def test_windows_npm_claude_is_reported_as_shim(tmp_path, monkeypatch):
    from acp_gateway.connectors import _discovery_probe as module

    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32", executable="python.exe"))
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm" / "claude.cmd").write_text("@node cli.js %*")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    result = module.inventory()
    assert result["agents"] == []
    assert result["shims"] == ["claude_code"]
