"""Native Proactor pipe ownership, no model or account access."""

import asyncio
import ctypes
import sys
from ctypes import wintypes

import pytest

from acp_gateway.adapters.codex import CodexAcpTransport
from acp_gateway.config import CodexSettings
from acp_gateway.connectors.policy import LocalAgentPolicy

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows pipe handles")


def process_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


@pytest.mark.parametrize("parent_exits", [False, True])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_native_owned_pipe_cleanup_does_not_wait_for_or_kill_descendant(
    tmp_path, parent_exits, cancel_caller
):
    api = process_api()
    settings = CodexSettings()
    policy = LocalAgentPolicy((str(tmp_path),))
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
    unrelated = None
    handle = None
    transport = None
    try:
        pid = int(await asyncio.wait_for(process.stdout.readline(), 5))
        handle = api.OpenProcess(0x1000 | 0x0001 | 0x100000, False, pid)
        assert handle, ctypes.get_last_error()
        unrelated = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)"
        )
        if parent_exits:
            async with asyncio.timeout(5):
                while process.returncode is None:
                    await asyncio.sleep(0.005)
        pipes = [process._transport.get_pipe_transport(fd) for fd in (0, 1, 2)]
        descriptors = [pipe.get_extra_info("pipe") for pipe in pipes]
        transport = CodexAcpTransport(process, settings, policy)
        close = asyncio.create_task(transport.close())
        if cancel_caller:
            await asyncio.sleep(0)
            close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close
            await asyncio.wait_for(asyncio.shield(transport._closing), 5)
        else:
            await asyncio.wait_for(close, 5)
        assert all(pipe.is_closing() for pipe in pipes)
        assert all(descriptor.handle is None for descriptor in descriptors)
        assert process._transport._finished
        assert process.returncode is not None
        assert all(task.done() for task in (transport._reader, transport._stderr, transport._pump))
        code = wintypes.DWORD()
        assert api.GetExitCodeProcess(handle, ctypes.byref(code))
        assert code.value == 259, "local cleanup killed a tool descendant"
        assert unrelated.returncode is None
    finally:
        if handle:
            api.TerminateProcess(handle, 1)
            api.WaitForSingleObject(handle, 5000)
            api.CloseHandle(handle)
        if unrelated is not None:
            if unrelated.returncode is None:
                unrelated.terminate()
            await asyncio.wait_for(unrelated.wait(), 5)
        if transport is not None:
            await transport.close()
        else:
            process._transport.close()
        await asyncio.wait_for(process.wait(), 5)
