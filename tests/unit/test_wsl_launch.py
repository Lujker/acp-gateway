"""Windows launch boundary tests; no global asyncio monkeypatches."""

# ruff: noqa: S108 -- fake launch values are never created on the filesystem
import importlib
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_non_windows_fails_closed():
    assert importlib.util.find_spec("acp_gateway.adapters.wsl") is not None
    module = importlib.import_module("acp_gateway.adapters.wsl")
    if sys.platform == "win32":
        pytest.skip("Linux boundary test")
    with pytest.raises(RuntimeError, match="Windows"):
        await module.spawn_wsl(
            SimpleNamespace(), ["/bin/cat"], cwd="/tmp", agent_home_env="CODEX_HOME", limit=65536
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("distribution", ["Ubuntu-22.04", "Custom Ubuntu"])
async def test_explicit_launch_isolated_environment(monkeypatch, distribution):
    import json

    from acp_gateway.adapters import wsl

    calls = []
    sentinel = SimpleNamespace(stderr=None)

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(wsl, "sys", SimpleNamespace(platform="win32"))
    import asyncio

    monkeypatch.setattr(
        wsl, "asyncio", SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": fake_exec})
    )
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("HOME", "SECRET_HOME")
    monkeypatch.setenv("WSLENV", "RELAY_TOKEN")
    monkeypatch.setenv("RELAY_TOKEN", "SECRET_TOKEN")
    settings = SimpleNamespace(
        distribution=distribution,
        user="tester",
        python="/usr/bin/python3",
        home="/tmp/profile",
        agent_home="/tmp/agent",
        path=["/home/tester/.local/bin"],
    )
    result = await wsl.spawn_wsl(
        settings,
        ["/opt/fake", "a ; $(bad)", "雪"],
        cwd="/tmp/work space",
        agent_home_env="CODEX_HOME",
        limit=1234,
    )
    assert result is sentinel
    args, kwargs = calls[0]
    assert args[:9] == (
        r"C:\Windows\System32\wsl.exe",
        "--distribution",
        distribution,
        "--user",
        "tester",
        "--cd",
        "/tmp/work space",
        "--exec",
        "/usr/bin/env",
    )
    assert args[9:15] == ("-i", "LANG=C.UTF-8", "/usr/bin/python3", "-I", "-u", "-c")
    data = json.loads(args[16])
    assert "cleanup_token" not in data
    assert data["argv"] == ["/opt/fake", "a ; $(bad)", "雪"]
    assert data["agent_home"] == "/tmp/agent"
    assert data["home"] == "/tmp/profile"
    assert data["path"] == ["/home/tester/.local/bin"]
    assert "SECRET" not in repr((args, kwargs))
    assert kwargs["env"] == {"SystemRoot": r"C:\Windows"}
    assert kwargs["limit"] == 1234


@pytest.mark.asyncio
async def test_cleanup_receipt_token_is_not_world_readable_launch_state(monkeypatch):
    """Same-UID agents can read /proc; the token must not sit in any cmdline."""
    import json

    from acp_gateway.adapters import wsl

    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdin=None, stdout=None, stderr=None, pid=1)

    import asyncio

    monkeypatch.setattr(wsl, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        wsl,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": fake_exec}),
    )
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    settings = SimpleNamespace(
        distribution="Ubuntu-22.04",
        user="tester",
        python="/usr/bin/python3",
        home="/tmp/profile",
        agent_home="/tmp/agent",
        path=[],
    )
    await wsl.spawn_wsl(
        settings,
        ["/opt/fake", "--version"],
        cwd="/tmp/work",
        agent_home_env="CODEX_HOME",
        limit=1024,
    )
    args = calls[0]
    assert not any(v.startswith("ACPGW_WSL_CLEANUP_TOKEN=") for v in args)
    assert args[0].endswith("wsl.exe")
    payload = json.loads(args[-1])
    assert set(payload) == {"home", "argv", "cwd", "agent_home", "agent_home_env", "path"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"argv": ["relative"]},
        {"argv": ["C:/fake"]},
        {"argv": ["//remote/fake"]},
        {"argv": ["/ok", "bad\x00value"]},
        {"argv": ["/ok", "x" * 33000]},
        {"cwd": "relative"},
        {"cwd": "/tmp/../bad"},
        {"agent_home_env": "RELAY_TOKEN"},
        {"limit": 0},
        {"distribution": "--exec"},
        {"user": ""},
        {"python": "python3"},
        {"path": ["C:/bin"]},
        {"path": ["/bad:path"]},
        {"agent_home": "relative"},
        {"root": "relative"},
        {"home": "relative"},
    ],
)
async def test_invalid_launch_fails_before_spawn(monkeypatch, change):
    from acp_gateway.adapters import wsl

    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return object()

    monkeypatch.setattr(wsl, "sys", SimpleNamespace(platform="win32"))
    import asyncio

    monkeypatch.setattr(
        wsl, "asyncio", SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": fake_exec})
    )
    monkeypatch.setenv("SystemRoot", change.get("root", r"C:\Windows"))
    settings = SimpleNamespace(
        distribution="Ubuntu", user="tester", python="/usr/bin/python3", agent_home=None, path=[]
    )
    params = dict(argv=["/bin/cat"], cwd="/tmp", agent_home_env="CODEX_HOME", limit=65536)
    for key, value in change.items():
        if key in params:
            params[key] = value
        elif key != "root":
            setattr(settings, key, value)
    with pytest.raises((ValueError, RuntimeError), match="WSL"):
        await wsl.spawn_wsl(settings, **params)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [False, True])
async def test_close_bounded_when_wrapper_lost(monkeypatch, lost):
    import asyncio

    from acp_gateway.adapters import wsl

    events = []

    class Pipe:
        def close(self):
            events.append("pipe-close")

    class Transport:
        def get_pipe_transport(self, fd):
            return Pipe()

        def close(self):
            events.append("transport-close")

    class Process:
        returncode = 99 if lost else None
        stdin = Pipe()
        _transport = Transport()

        async def wait(self):
            events.append("wait")
            if self.returncode is None:
                await asyncio.Future()
            return self.returncode

        def terminate(self):
            events.append("terminate")
            self.returncode = 99

        def kill(self):
            events.append("kill")
            self.returncode = 99

    monkeypatch.setattr(wsl, "_CLOSE_TIMEOUT", 0.01, raising=False)
    assert await asyncio.wait_for(wsl.close_wsl(Process()), 1) is False
    assert events[0] == "pipe-close"
    assert events.count("pipe-close") == 4
    assert "transport-close" in events
    assert ("terminate" in events) is not lost


@pytest.mark.asyncio
async def test_cleanup_receipt_is_private_bounded_and_required():
    import asyncio

    from acp_gateway.adapters import wsl

    token = b"0123456789abcdef0123456789abcdef"
    p = SimpleNamespace(stderr=asyncio.StreamReader())
    wsl._watch_cleanup(p)
    for byte in b"\x1eACPGW_WSL_BEGIN:" + token + b"\n":
        p.stderr.feed_data(bytes([byte]))
    p.stderr.feed_data(b"native diagnostics\n")
    assert p._wsl_cleanup_verified is False
    for byte in b"\x1eACPGW_WSL_CLEAN:" + token + b"\n":
        p.stderr.feed_data(bytes([byte]))
    p.stderr.feed_eof()
    assert p._wsl_cleanup_verified is True
    assert await p.stderr.read() == b"native diagnostics\n"


@pytest.mark.asyncio
async def test_normal_wrapper_exit_without_receipt_is_uncertain():
    import asyncio

    from acp_gateway.adapters.wsl import close_wsl

    p = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "pass",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert await close_wsl(p) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("already_eof", [False, True])
async def test_completion_before_spawn_returns_is_verified(already_eof):
    import asyncio

    from acp_gateway.adapters import wsl

    reader = asyncio.StreamReader()
    reader.feed_data(
        b"\x1eACPGW_WSL_BEGIN:0123456789abcdef0123456789abcdef\n"
        b"diagnostic\n\x1eACPGW_WSL_CLEAN:0123456789abcdef0123456789abcdef\n"
    )
    if already_eof:
        reader.feed_eof()
    p = SimpleNamespace(stderr=reader)
    wsl._watch_cleanup(p)
    if not already_eof:
        reader.feed_eof()
    assert p._wsl_cleanup_verified is True
    assert await reader.read() == b"diagnostic\n"


@pytest.mark.asyncio
async def test_launcher_failure_diagnostics_are_generic(monkeypatch):
    from acp_gateway.adapters import wsl

    async def failure(*args, **kwargs):
        raise FileNotFoundError("SECRET_EXECUTABLE_DETAILS")

    monkeypatch.setattr(wsl, "sys", SimpleNamespace(platform="win32"))
    import asyncio

    monkeypatch.setattr(
        wsl, "asyncio", SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": failure})
    )
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    settings = SimpleNamespace(
        distribution="Ubuntu", user="tester", python="/usr/bin/python3", agent_home=None, path=[]
    )
    with pytest.raises(RuntimeError, match=r"^WSL subprocess launch failed$"):
        await wsl.spawn_wsl(
            settings, ["/bin/cat"], cwd="/work", agent_home_env="CODEX_HOME", limit=65536
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "missing-begin",
        "native-before-begin",
        "bad-token",
        "short-token",
        "long-token",
        "wrong-clean",
        "duplicate-begin",
        "duplicate-clean",
        "clean-before-begin",
        "overlong",
        "partial",
        "missing-clean",
        "native-after-clean",
    ],
)
async def test_untrusted_cleanup_frames_never_verify(case, buffered):
    import asyncio

    from acp_gateway.adapters import wsl

    token = b"0123456789abcdef0123456789abcdef"
    begin = b"\x1eACPGW_WSL_BEGIN:" + token + b"\n"
    clean = b"\x1eACPGW_WSL_CLEAN:" + token + b"\n"
    cases = {
        "missing-begin": clean,
        "native-before-begin": b"native\n" + begin + clean,
        "bad-token": begin.replace(token, b"Z" * 32) + clean,
        "short-token": begin.replace(token, token[:-1]) + clean,
        "long-token": begin.replace(token, token + b"0") + clean,
        "wrong-clean": begin + clean.replace(token, b"f" * 32),
        "duplicate-begin": begin + begin + clean,
        "duplicate-clean": begin + clean + clean,
        "clean-before-begin": clean + begin + clean,
        "overlong": begin + b"\x1eACPGW_WSL_" + b"X" * 1000000 + b"\n" + clean,
        "partial": begin + clean[:-1],
        "missing-clean": begin + b"native\n",
        "native-after-clean": begin + clean + b"native\n",
    }
    data = cases[case]
    reader = asyncio.StreamReader()
    if buffered:
        reader.feed_data(data)
        reader.feed_eof()
    p = SimpleNamespace(stderr=reader)
    wsl._watch_cleanup(p)
    if not buffered:
        for offset in range(0, len(data), 17):
            reader.feed_data(data[offset : offset + 17])
        assert p._wsl_cleanup_verified is False
        reader.feed_eof()
    assert p._wsl_cleanup_verified is False
    native = await reader.read()
    assert native == (
        b"native\n"
        if case in {"native-before-begin", "missing-clean", "native-after-clean"}
        else b""
    )


@pytest.mark.asyncio
async def test_partial_initial_handshake_then_native_stderr_is_preserved():
    import asyncio

    from acp_gateway.adapters import wsl

    reader = asyncio.StreamReader()
    reader.feed_data(b"\x1eACPGW_")
    p = SimpleNamespace(stderr=reader)
    wsl._watch_cleanup(p)
    reader.feed_data(b"native-not-a-frame\nmore diagnostic\n")
    reader.feed_eof()
    assert p._wsl_cleanup_verified is False
    assert await reader.read() == b"\x1eACPGW_native-not-a-frame\nmore diagnostic\n"
