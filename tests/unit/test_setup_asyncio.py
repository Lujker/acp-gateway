"""Deterministic real CPython Proactor callback failure; no remote enrollment."""

import ast
import asyncio
import sys
from asyncio.proactor_events import _ProactorBasePipeTransport
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp_gateway.cli import setup_asyncio


@pytest.mark.parametrize("debug", [False, True])
def test_exact_closing_callback_has_private_capture(monkeypatch, caplog, debug):
    monkeypatch.setattr(setup_asyncio, "sys", SimpleNamespace(platform="win32"))
    error = OSError("SECRET token /private/path")
    error.winerror = 10022

    class Socket:
        closed = False

        def fileno(self):
            return 123

        def shutdown(self, how):
            raise error

        def close(self):
            self.closed = True

    sock = Socket()
    seen = []

    async def operation():
        loop = asyncio.get_running_loop()
        loop.set_debug(debug)
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.ProactorEventLoop)
        transport = _ProactorBasePipeTransport(loop, sock, asyncio.Protocol())
        seen.append(transport)
        transport.close()
        await asyncio.sleep(0)
        return "enrollment-result"

    try:
        assert setup_asyncio.run(operation(), operation="enrollment-begin") == "enrollment-result"
        assert "operation=enrollment-begin" in caplog.text
        assert "transport=pipe" in caplog.text
        assert "python=" in caplog.text
        assert "WinError 10022" in caplog.text
        assert "cleanup incomplete" in caplog.text
        assert "capture-only" in caplog.text
        assert "SECRET" not in caplog.text
        assert "/private/path" not in caplog.text
        assert "_ProactorBasePipeTransport object" not in caplog.text
        assert 'File "' not in caplog.text
        assert "handle_traceback" not in caplog.text
        # Reporting is NOT cleanup repair: don't lie about or mutate CPython state.
        assert not sock.closed
        assert not seen[0]._called_connection_lost
    finally:
        sock.close()
        seen[0]._sock = None


def test_normal_coroutine_exception_propagates():
    async def operation():
        raise ValueError("operation failed")

    with pytest.raises(ValueError, match="operation failed"):
        setup_asyncio.run(operation())


def test_protocol_error_is_not_a_shutdown_failure(monkeypatch, caplog):
    monkeypatch.setattr(setup_asyncio, "sys", SimpleNamespace(platform="win32"))
    error = OSError("protocol failure must remain visible")
    error.winerror = 10022

    class Protocol(asyncio.Protocol):
        def connection_lost(self, exc):
            raise error

    class Pipe:
        def close(self):
            pass

    async def operation():
        transport = _ProactorBasePipeTransport(asyncio.get_running_loop(), Pipe(), Protocol())
        transport.close()
        await asyncio.sleep(0)

    setup_asyncio.run(operation())
    assert "protocol failure must remain visible" in caplog.text
    assert "capture-only" not in caplog.text


@pytest.mark.parametrize(
    ("module", "operations"),
    [
        ("wizard", {"enrollment-cancel", "enrollment-begin", "enrollment-finish", "checks"}),
        ("hermes_setup", {"hermes-inspect"}),
        ("probe", {"goose-probe", "hermes-probe"}),
        ("doctor", {"doctor-acp", "doctor-codex", "doctor-claude-code"}),
    ],
)
def test_all_setup_async_boundaries_use_owned_runner(module, operations):
    tree = ast.parse((Path(setup_asyncio.__file__).parent / f"{module}.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "asyncio"
        and call.func.attr == "run"
        for call in calls
    ), "raw asyncio.run bypasses setup diagnostics"
    actual = [
        kw.value.value
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "run_setup"
        for kw in call.keywords
        if kw.arg == "operation" and isinstance(kw.value, ast.Constant)
    ]
    assert set(actual) == operations
    assert len(actual) == len(operations)


@pytest.mark.parametrize(
    "case",
    [
        "other-winerror",
        "errno-only",
        "other-error",
        "other-callback",
        "not-closing",
        "not-windows",
        "no-handle",
        "already-cleaned",
    ],
)
def test_nonmatching_context_delegates_unchanged(monkeypatch, case):
    error = OSError("unrelated")
    error.winerror = 10022
    transport = object.__new__(_ProactorBasePipeTransport)
    transport._closing = True
    transport._sock = object()
    transport._called_connection_lost = False
    callback = transport._call_connection_lost
    platform = "win32"
    if case == "other-winerror":
        error.winerror = 10054
    elif case == "errno-only":
        error = OSError(10022, "not a Windows winerror")
    elif case == "other-error":
        error = ValueError("not an OSError")
    elif case == "other-callback":
        callback = transport.close
    elif case == "not-closing":
        transport._closing = False
    elif case == "not-windows":
        platform = "linux"
    elif case == "already-cleaned":
        transport._sock = None
        transport._called_connection_lost = True
    monkeypatch.setattr(setup_asyncio, "sys", SimpleNamespace(platform=platform))
    handle = SimpleNamespace(_callback=callback) if case != "no-handle" else None
    context = {"exception": error, "handle": handle, "message": "untouched"}
    delegated = []
    try:
        setup_asyncio._exception_handler(
            SimpleNamespace(default_exception_handler=delegated.append), context
        )
        assert len(delegated) == 1
        assert delegated[0] is context
    finally:
        transport._sock = None


def test_capture_survives_runner_shutdown_and_restores_handler(monkeypatch, caplog):
    monkeypatch.setattr(setup_asyncio, "sys", SimpleNamespace(platform="win32"))
    loops = []
    transports = []
    cancelled = []
    error = OSError("SECRET shutdown")
    error.winerror = 10022

    class Socket:
        def shutdown(self, how):
            raise error

        def fileno(self):
            return 123

        def close(self):
            pass

    async def pending():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
            transport = _ProactorBasePipeTransport(
                asyncio.get_running_loop(), Socket(), asyncio.Protocol()
            )
            transports.append(transport)
            transport.close()

    async def operation():
        loops.append(asyncio.get_running_loop())
        task = asyncio.create_task(pending())
        await asyncio.sleep(0)
        assert not task.done()

    try:
        setup_asyncio.run(operation(), operation="SECRET /private/path")
        assert cancelled == [True]
        assert loops[0].is_closed()
        assert loops[0].get_exception_handler() is None
        assert "operation=setup" in caplog.text
        assert "SECRET" not in caplog.text
        assert "/private/path" not in caplog.text
    finally:
        for transport in transports:
            transport._sock = None


@pytest.mark.parametrize("failure", [ValueError("ordinary"), OSError("operation 10022")])
def test_windows_coroutine_failure_propagates(monkeypatch, failure):
    monkeypatch.setattr(setup_asyncio, "sys", SimpleNamespace(platform="win32"))
    if isinstance(failure, OSError):
        failure.winerror = 10022
    loops = []

    async def operation():
        loops.append(asyncio.get_running_loop())
        raise failure

    with pytest.raises(type(failure)) as caught:
        setup_asyncio.run(operation())
    assert caught.value is failure
    assert loops[0].is_closed()
    assert loops[0].get_exception_handler() is None
