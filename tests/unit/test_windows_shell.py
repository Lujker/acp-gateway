"""Windows shell orchestration contracts; no native process claims on other OSes."""

import asyncio
import base64
import ctypes
import io
from types import SimpleNamespace

import pytest

from acp_gateway.connectors import _shell_worker, _windows_shell_job, windows_shell
from acp_gateway.connectors.policy import LocalPolicyError
from acp_gateway.connectors.shell_runner import ShellRunner, worker_source


class Job:
    name = "Local\\ACPGW-Shell-" + "a" * 64

    def __init__(self, name=None):
        self.calls = []
        self.counts = [1, 0, 0]

    def terminate(self):
        self.calls.append("terminate")

    def active(self):
        self.calls.append("active")
        return self.counts.pop(0) if self.counts else 0

    def close(self):
        self.calls.append("close")


class Process:
    def __init__(self, ready=None):
        self._shell_windows_job = Job()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(
            ready if ready is not None else ("ACPGW_SHELL_READY:" + Job.name + "\n").encode()
        )
        self.stdout.feed_eof()
        self.written = []
        self.stdin = SimpleNamespace(
            write=self.written.append, drain=self.drain, close=lambda: None
        )
        self._transport = SimpleNamespace(
            close=lambda: self._shell_windows_job.calls.append("pipes")
        )
        self.returncode = 0

    async def drain(self):
        pass

    async def wait(self):
        self._shell_windows_job.calls.append("wait")
        return self.returncode


def test_environment_and_unicode_powershell_payload_are_local_and_bounded(monkeypatch):
    monkeypatch.setattr(
        windows_shell.os,
        "environ",
        {
            "SystemRoot": r"C:\Windows",
            "PATH": r"C:\tools",
            "TEMP": r"C:\Temp",
            "TELEGRAM_BOT_TOKEN": "never inherited",
            "ACPGW_CONNECTOR_KEY": "never inherited",
            "CODEX_HOME": "never inherited",
            "PYTHONPATH": "never inherited",
        },
    )
    env, _ = windows_shell.shell_environment()
    assert "TELEGRAM_BOT_TOKEN" not in env and "ACPGW_CONNECTOR_KEY" not in env
    assert "CODEX_HOME" not in env and "PYTHONPATH" not in env
    argv = windows_shell.shell_argv("Write-Output '雪'; exit 7")
    assert argv[0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    script = base64.b64decode(argv[-1]).decode("utf-16le")
    assert script.endswith("Write-Output '雪'; exit 7") and "UTF8Encoding" in script
    assert "-NoProfile" in argv and "-NonInteractive" in argv


@pytest.mark.parametrize("root", ["", "relative", r"\\server\Windows", r"C:\bad\..\Windows"])
def test_missing_or_nonlocal_systemroot_refuses_before_spawn(monkeypatch, root):
    monkeypatch.setattr(windows_shell.os, "environ", {"SystemRoot": root})
    with pytest.raises(LocalPolicyError):
        windows_shell.shell_environment()


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
async def test_command_is_written_only_after_exact_worker_ready(ending):
    process = Process(("ACPGW_SHELL_READY:" + Job.name).encode() + ending)
    await ShellRunner._exchange(process, b"payload\n", None)
    assert process.written == [b"payload\n"]


@pytest.mark.parametrize("line", [b"", b"ready\n", b'{"error":"spawn_failed"}\n'])
async def test_bad_or_absent_ready_never_receives_execution_payload(line):
    process = Process(line)
    with pytest.raises(ValueError):
        await ShellRunner._exchange(process, b"payload\n", None)
    assert not process.written


async def test_cancellation_between_ready_and_write_sends_no_command():
    process = Process()
    cancel = asyncio.Event()
    cancel.set()
    await ShellRunner._exchange(process, b"payload\n", cancel)
    assert not process.written


async def test_cleanup_requires_empty_job_and_launcher_exit_before_success():
    process = Process()
    assert await windows_shell.close_windows(process)
    calls = process._shell_windows_job.calls
    assert calls[:4] == ["terminate", "active", "active", "wait"]
    assert calls.index("wait") < calls.index("close") < calls.index("pipes")


async def test_failed_cleanup_cannot_claim_verified():
    process = Process()

    def failed():
        raise OSError("job query failed")

    process._shell_windows_job.active = failed
    assert not await windows_shell.close_windows(process)
    assert process._shell_windows_job.calls[-2:] == ["pipes", "wait"]


@pytest.mark.parametrize("frozen", [False, True])
async def test_source_and_frozen_launch_have_same_job_gate(monkeypatch, frozen):
    monkeypatch.setattr(windows_shell.os, "environ", {"SystemRoot": r"C:\Windows"})
    monkeypatch.setattr(windows_shell.sys, "frozen", frozen, raising=False)
    created = []
    job = Job()
    monkeypatch.setattr(windows_shell, "WindowsShellJob", lambda name: job)

    async def spawn(*args, **kwargs):
        created.append((args, kwargs))
        return Process()

    monkeypatch.setattr(windows_shell.asyncio, "create_subprocess_exec", spawn)
    await windows_shell.spawn_windows(worker_source(windows=True), r"C:\work", 1048576)
    args, kwargs = created[0]
    assert args[-1] == job.name
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert ("__shell-worker" in args) is frozen
    assert len(" ".join(args)) < 30000


async def test_failed_spawn_closes_job_without_sending_payload(monkeypatch):
    monkeypatch.setattr(windows_shell.os, "environ", {"SystemRoot": r"C:\Windows"})
    job = Job()
    monkeypatch.setattr(windows_shell, "WindowsShellJob", lambda name: job)

    async def failed(*args, **kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(windows_shell.asyncio, "create_subprocess_exec", failed)
    with pytest.raises(OSError):
        await windows_shell.spawn_windows("source", r"C:\work", 1048576)
    assert job.calls == ["close"]


class API:
    def __init__(self, *, assign=True, exists=False, configure=True):
        self.calls = []
        self.assign, self.exists, self.configure = assign, exists, configure

    def CreateJobObjectW(self, security, name):
        self.calls.append("create")
        return 42

    def SetInformationJobObject(self, handle, kind, pointer, size):
        limits = ctypes.cast(pointer, ctypes.POINTER(_windows_shell_job._Extended)).contents
        assert kind == 9 and limits.basic.flags == 0x2000
        self.calls.append("limits")
        return self.configure

    def OpenJobObjectW(self, rights, inherit, name):
        assert rights == 5 and not inherit
        self.calls.append("open")
        return 42

    def GetCurrentProcess(self):
        return 99

    def AssignProcessToJobObject(self, handle, process):
        assert handle == 42 and process == 99
        self.calls.append("assign")
        return self.assign

    def IsProcessInJob(self, process, handle, assigned):
        assigned._obj.value = 1
        self.calls.append("verify")
        return True

    def CloseHandle(self, handle):
        assert handle == 42
        self.calls.append("close")
        return True


def fake_api(monkeypatch, **kwargs):
    api = API(**kwargs)
    monkeypatch.setattr(_windows_shell_job, "_api", lambda: api)
    monkeypatch.setattr(ctypes, "set_last_error", lambda code: None, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 183 if api.exists else 0, raising=False)
    return api


@pytest.mark.parametrize("exists,configure", [(True, True), (False, False)])
def test_existing_or_unconfigured_job_is_closed_without_termination(monkeypatch, exists, configure):
    api = fake_api(monkeypatch, exists=exists, configure=configure)
    with pytest.raises(OSError):
        _windows_shell_job.WindowsShellJob(Job.name)
    assert api.calls[-1] == "close"
    assert "terminate" not in api.calls


def test_worker_closes_own_job_handle_before_command_gate(monkeypatch):
    api = fake_api(monkeypatch)
    _windows_shell_job.enter_windows_job(Job.name)
    assert api.calls == ["open", "assign", "verify", "close"]


def test_failed_membership_never_emits_ready_or_reads_command(monkeypatch):
    api = fake_api(monkeypatch, assign=False)
    stdout = io.StringIO()

    class ExitWorker(Exception):
        pass

    def exit_worker(code):
        raise ExitWorker

    def forbidden(*args, **kwargs):
        pytest.fail("command must not be read or executed before job membership")

    monkeypatch.setattr(
        _shell_worker,
        "sys",
        SimpleNamespace(
            platform="win32",
            stdout=stdout,
            stdin=SimpleNamespace(buffer=SimpleNamespace(readline=forbidden)),
        ),
    )
    monkeypatch.setattr(_shell_worker, "os", SimpleNamespace(_exit=exit_worker))
    monkeypatch.setattr(_shell_worker.subprocess, "Popen", forbidden)
    with pytest.raises(ExitWorker):
        _shell_worker.main(enter_job=_windows_shell_job.enter_windows_job, job_name=Job.name)
    assert "READY" not in stdout.getvalue() and "spawn_failed" in stdout.getvalue()
    assert api.calls == ["open", "assign", "close"]
