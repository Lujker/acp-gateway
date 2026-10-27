"""Native Windows job ownership with synthetic commands, no model/account access."""

import asyncio
import ctypes
import os
import sys
from ctypes import wintypes

import pytest

from acp_gateway.config import AgentProfile
from acp_gateway.connectors.shell_runner import ShellRunner

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows shell jobs")


def runner(tmp_path):
    return ShellRunner(
        AgentProfile(
            alias="code",
            kind="codex",
            backend="codex",
            default_cwd=str(tmp_path),
            codex={"mode": "work"},
        )
    )


async def test_native_shell_unicode_stderr_exit_and_noninteractive_input(tmp_path):
    result = await runner(tmp_path).run(
        "[Console]::WriteLine('雪'); [Console]::Error.WriteLine('error'); exit 7", str(tmp_path)
    )
    assert result["state"] == "failed" and result["exit_code"] == 7
    assert result["cleanup_verified"] and "雪" in result["output"]
    assert "[stderr]\nerror" in result["output"]


async def test_native_shell_does_not_inherit_gateway_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-value")
    monkeypatch.setenv("ACPGW_CONNECTOR_KEY", "test-value")
    result = await runner(tmp_path).run(
        "if ($env:TELEGRAM_BOT_TOKEN -or $env:ACPGW_CONNECTOR_KEY) { exit 9 }; "
        "[Console]::Write('clean')",
        str(tmp_path),
    )
    assert result["state"] == "completed" and result["output"] == "clean"


async def test_native_timeout_is_verified_and_does_not_publish_partial_output(tmp_path):
    result = await runner(tmp_path).run(
        "[Console]::Write('partial'); Start-Sleep -Seconds 30", str(tmp_path), timeout=0.5
    )
    assert result["state"] == "timed_out" and result["cleanup_verified"]
    assert not result["output"]


@pytest.mark.parametrize("cancelled", [False, True])
async def test_native_job_cleans_descendant_and_leaves_unrelated_process_alone(tmp_path, cancelled):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    unrelated = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", "import time; time.sleep(30)"
    )
    cancel = asyncio.Event()
    command = (
        "$exe = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'; "
        "$child = Start-Process -FilePath $exe -ArgumentList "
        "'-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 30' -PassThru; "
        "[IO.File]::WriteAllText((Join-Path (Get-Location) 'child.pid'), [string]$child.Id); "
        + (
            "Start-Sleep -Seconds 30"
            if cancelled
            else "while (!(Test-Path 'release')) { Start-Sleep -Milliseconds 20 }; exit 0"
        )
    )
    pending = asyncio.create_task(runner(tmp_path).run(command, str(tmp_path), cancel=cancel))
    handle = None
    try:
        async with asyncio.timeout(10):
            while not (tmp_path / "child.pid").exists():
                if pending.done():
                    pytest.fail(f"worker exited before creating descendant: {pending.result()}")
                await asyncio.sleep(0.01)
            pid = int((tmp_path / "child.pid").read_text())
            handle = api.OpenProcess(0x100000 | 0x0001, False, pid)
            assert handle
            if cancelled:
                cancel.set()
            else:
                (tmp_path / "release").touch()
            result = await pending
        assert result["state"] == ("cancelled" if cancelled else "completed")
        assert result["cleanup_verified"]
        if handle:
            assert api.WaitForSingleObject(handle, 3000) == 0
        assert unrelated.returncode is None
    finally:
        cancel.set()
        await asyncio.wait_for(pending, 10)
        if handle:
            api.TerminateProcess(handle, 1)
            api.WaitForSingleObject(handle, 3000)
            api.CloseHandle(handle)
        if unrelated.returncode is None:
            unrelated.kill()
        await unrelated.wait()


async def test_internal_worker_entry_uses_job_gate_before_payload(tmp_path):
    # The source interpreter drives the same hidden CLI entry as a frozen child.
    # The actual PyInstaller archive still needs a Windows binary smoke test.
    from acp_gateway.connectors._windows_shell_job import WindowsShellJob

    job = WindowsShellJob("Local\\ACPGW-Shell-" + os.urandom(32).hex())
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "acp_gateway",
        "__shell-worker",
        job.name,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        ready = await asyncio.wait_for(process.stdout.readline(), 10)
        assert ready.strip() == ("ACPGW_SHELL_READY:" + job.name).encode()
        assert job.active() == 1
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        assert job.active() == 0
    finally:
        job.terminate()
        job.close()
        if process.returncode is None:
            process.kill()
        await process.wait()


def test_win64_job_structure_sizes_match_win32_abi():
    from acp_gateway.connectors._windows_shell_job import _Accounting, _Extended, _Limits

    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("64-bit Windows structure layout")
    assert ctypes.sizeof(_Limits) == 64
    assert ctypes.sizeof(_Extended) == 144
    assert ctypes.sizeof(_Accounting) == 48
