"""Native Windows shell launch and bounded, evidence-based job cleanup."""

import asyncio
import base64
import contextlib
import os
import secrets
import sys
from pathlib import PureWindowsPath

from acp_gateway.connectors._windows_shell_job import WindowsShellJob
from acp_gateway.connectors.policy import LocalPolicyError


def shell_environment():
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMSPEC",
        "PATHEXT",
        "PATH",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    root = next((v for k, v in env.items() if k.upper() == "SYSTEMROOT"), "")
    path = PureWindowsPath(root)
    if not root or not path.is_absolute() or len(path.drive) != 2 or ".." in path.parts:
        raise LocalPolicyError("Windows system directory is unavailable")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env, path


def shell_argv(command):
    _, root = shell_environment()
    executable = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);\n" + command
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


async def spawn_windows(source, cwd, limit):
    env, _ = shell_environment()
    job = WindowsShellJob("Local\\ACPGW-Shell-" + secrets.token_hex(32))
    argv = (
        [sys.executable, "__shell-worker", job.name]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-I", "-u", "-c", source, job.name]
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000,
            limit=limit,  # CREATE_NO_WINDOW
        )
    except BaseException:
        job.close()
        raise
    process._shell_windows_job = job
    return process


async def windows_ready(process):
    job = process._shell_windows_job
    expected = ("ACPGW_SHELL_READY:" + job.name + "\n").encode("ascii")
    if await process.stdout.readline() not in {expected, expected[:-1] + b"\r\n"}:
        raise ValueError("Windows shell worker did not confirm job membership")


async def close_windows(process):
    """Termination alone is insufficient: require empty accounting and wrapper exit."""
    job = process._shell_windows_job
    verified = False
    try:
        if process.stdin:
            process.stdin.close()
        job.terminate()
        async with asyncio.timeout(5):
            while job.active():
                await asyncio.sleep(0.02)
            await process.wait()
            verified = job.active() == 0
    except (TimeoutError, OSError, ValueError, ConnectionError):
        pass
    finally:
        try:
            job.close()  # KILL_ON_JOB_CLOSE remains a last-resort owner-death fence.
        except OSError:
            verified = False
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()  # Owned launcher only; not a PID/name search.
        # Close owned Proactor pipes even if a descendant outside the job retained one.
        process._transport.close()
        with contextlib.suppress(TimeoutError, OSError):
            await asyncio.wait_for(process.wait(), 2)
    return verified
