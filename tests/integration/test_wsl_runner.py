"""Real Linux supervisor tests with synthetic peers, never model calls."""

# ruff: noqa: S108 -- invalid-path fixtures do not create shared temporary files
import asyncio
import contextlib
import ctypes
import importlib.resources
import json
import os
import platform
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux supervisor")


def pidfd_open(pid):
    if platform.machine() not in {"x86_64", "aarch64"}:
        pytest.skip("pidfd syscall shim supports x86_64/aarch64")
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(434, pid, 0)
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return fd


def pidfd_kill(fd):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(424, fd, 9, ctypes.c_void_p(), 0) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def source():
    return (
        importlib.resources.files("acp_gateway.adapters")
        .joinpath("_wsl_runner.py")
        .read_text(encoding="utf-8")
    )


async def launch(
    code, tmp_path, *, runner_python=None, prelude="", limit=65536, watch=True, **overrides
):
    data = {
        "argv": [sys.executable, "-I", "-u", "-c", code],
        "cwd": str(tmp_path),
        "agent_home": None,
        "agent_home_env": "CODEX_HOME",
        "path": [],
    }
    data.update(overrides)
    p = await asyncio.create_subprocess_exec(
        runner_python or sys.executable,
        "-I",
        "-u",
        "-c",
        prelude + source(),
        json.dumps(data),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"LANG": "C.UTF-8"},
        limit=limit,
    )
    from acp_gateway.adapters import wsl

    if watch:
        wsl._watch_cleanup(p)
    return p


@pytest.mark.asyncio
async def test_unicode_roundtrip_eof(tmp_path):
    p = await launch("import sys; [print(s.strip(), flush=True) for s in sys.stdin]", tmp_path)
    try:
        p.stdin.write("雪☃\n".encode())
        await p.stdin.drain()
        assert await asyncio.wait_for(p.stdout.readline(), 5) == "雪☃\n".encode()
        p.stdin.close()
        assert await asyncio.wait_for(p.wait(), 5) == 0
    finally:
        if p.returncode is None:
            p.kill()
            await p.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["eof", "exit", "term"])
async def test_owned_group_cleanup_keeps_sibling(tmp_path, ending):
    import signal

    sibling = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time;time.sleep(60)"
    )
    code = (
        "import os,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "c=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']); "
        "print(str(os.getpid())+' '+str(c.pid),flush=True); "
        + ("sys.exit(0)" if ending == "exit" else "time.sleep(60)")
    )
    p = await launch(code, tmp_path)
    owned = []
    handles = []
    try:
        owned = list(map(int, (await asyncio.wait_for(p.stdout.readline(), 5)).split()))
        for pid in owned:
            with contextlib.suppress(ProcessLookupError):
                handles.append(pidfd_open(pid))
        assert len(owned) == 2
        if ending == "eof":
            p.stdin.close()
        elif ending == "term":
            p.send_signal(signal.SIGTERM)
        await asyncio.wait_for(p.wait(), 5)
        for pid in owned:
            stat = f"/proc/{pid}/stat"
            if Path(stat).exists():
                with Path(stat).open() as f:
                    assert f.read().split()[2] == "Z"
        assert sibling.returncode is None
    finally:
        if p.returncode is None:
            p.kill()
            await p.wait()
        for handle in handles:
            with contextlib.suppress(ProcessLookupError):
                pidfd_kill(handle)
            os.close(handle)
        sibling.kill()
        await sibling.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["stdout", "stderr", "stdin"])
async def test_backpressure_has_bounded_cleanup(tmp_path, channel):
    code = "import os,time; " + (
        "time.sleep(60)"
        if channel == "stdin"
        else f"\nwhile True: os.write({1 if channel == 'stdout' else 2},b'x'*65536)"
    )
    p = await launch(code, tmp_path)
    try:
        if channel == "stdin":
            p.stdin.write(b"x" * 1048576)
        # Do not consume child output. Check wrapper exit without Process.wait(),
        # which also waits for local pipe completion when inherited pipes fill.
        for _ in range(100):
            if p.returncode is not None:
                break
            await asyncio.sleep(0.05)
        assert p.returncode is not None
    finally:
        if p.returncode is None:
            p.kill()
        await p.communicate()


@pytest.mark.asyncio
async def test_stalled_consumer_is_reported_as_unverified_not_clean(tmp_path):
    """A 2 s stall kills the owned child; that must never look like a clean exit."""

    code = (
        "import os,time\n"
        "os.write(1, b'A' * 400000)\n"
        "os.write(2, b'child-still-running\\n')\n"
        "time.sleep(30)\n"
        "os.write(1, b'CHILD-FINISHED\\n')\n"
    )
    p = await launch(code, tmp_path)
    try:
        # Mirror a stalled downstream reader: nothing is consumed for >2 s, then
        # everything is drained. The supervisor's bounded queue overflows, it
        # kills the owned child, and the stream is truncated.
        await asyncio.sleep(4)
        stdout, stderr = await asyncio.wait_for(p.communicate(), 20)
        assert p.returncode is not None
        assert p.returncode != 0, "a killed live child must not look like a clean exit"
        assert getattr(p, "_wsl_cleanup_verified", False) is False
    finally:
        if p.returncode is None:
            p.kill()
            await p.communicate()
    assert b"CHILD-FINISHED" not in stdout
    assert b"ACPGW_WSL_CLEAN" not in stderr


@pytest.mark.asyncio
async def test_close_waits_for_supervisor_and_closes_local_pipes(tmp_path):
    from acp_gateway.adapters import wsl

    assert hasattr(wsl, "close_wsl")
    p = await launch("import os,time;print(os.getpid(),flush=True);time.sleep(60)", tmp_path)
    try:
        pid = int(await asyncio.wait_for(p.stdout.readline(), 5))
        transports = [p._transport.get_pipe_transport(i) for i in (0, 1, 2)]
        pipes = [t.get_extra_info("pipe") for t in transports]
        assert await wsl.close_wsl(p) is True
        assert not Path(f"/proc/{pid}").exists()
        assert p.returncode == 0
        assert all(t.is_closing() for t in transports)
        for pipe in pipes:
            with pytest.raises(ValueError):
                pipe.fileno()
    finally:
        if p.returncode is None:
            p.stdin.close()
            await asyncio.wait_for(p.wait(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("home_name", ["CODEX_HOME", "CLAUDE_CONFIG_DIR"])
@pytest.mark.parametrize("explicit_home", [False, True])
async def test_linux_identity_environment_and_cwd(tmp_path, home_name, explicit_home):
    import pwd

    code = (
        "import os,json; print(json.dumps(dict(env=dict(os.environ),cwd=os.getcwd())),flush=True)"
    )
    home = str(tmp_path / "profile") if explicit_home else pwd.getpwuid(os.getuid()).pw_dir
    p = await launch(
        code,
        tmp_path,
        home=home if explicit_home else None,
        agent_home=str(tmp_path / "isolated"),
        agent_home_env=home_name,
        path=["/home/tester/.local/bin"],
    )
    line = await asyncio.wait_for(p.stdout.readline(), 5)
    data = json.loads(line)
    p.stdin.close()
    await asyncio.wait_for(p.wait(), 5)
    assert data["cwd"] == str(tmp_path)
    assert data["env"] == {
        "HOME": home,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin:/home/tester/.local/bin",
        "LANG": "C.UTF-8",
        home_name: str(tmp_path / "isolated"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"argv": ["relative"]},
        {"agent_home_env": "PATH", "agent_home": "/tmp"},
        {"path": ["/tmp:evil"]},
        {"cwd": "relative"},
        {"home": "relative"},
    ],
)
async def test_runner_defensive_validation_is_generic(tmp_path, change):
    p = await launch("print('UNSAFE_CHILD')", tmp_path, **change)
    stdout, stderr = await asyncio.wait_for(p.communicate(), 5)
    assert p.returncode == 125
    assert stdout == b""
    assert stderr == b"WSL subprocess failed\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("abrupt", [False, True])
async def test_runner_loss_cleans_immediate_child(tmp_path, abrupt):
    import signal

    p = await launch("import os,time;print(os.getpid(),flush=True);time.sleep(60)", tmp_path)
    pid = int(await asyncio.wait_for(p.stdout.readline(), 5))
    handle = pidfd_open(pid)
    try:
        p.send_signal(signal.SIGKILL if abrupt else signal.SIGHUP)
        await asyncio.wait_for(p.wait(), 5)
        for _ in range(100):
            stat = f"/proc/{pid}/stat"
            if not Path(stat).exists():
                break
            try:
                with Path(stat).open() as f:
                    if f.read().split()[2] == "Z":
                        break
            except (FileNotFoundError, ProcessLookupError):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("owned child survived runner loss")
    finally:
        with contextlib.suppress(ProcessLookupError):
            pidfd_kill(handle)
        os.close(handle)


@pytest.mark.asyncio
async def test_native_exit_drains_bounded_pending_stdout(tmp_path):
    import signal

    gate = str(tmp_path / "gate")
    code = (
        "import os,time,pathlib;print(os.getpid(),flush=True);"
        f"\nwhile not pathlib.Path({gate!r}).exists(): time.sleep(.01)"
        "\nos.write(1,b'q'*2000)"
    )
    p = await launch(code, tmp_path)
    pid = int(await asyncio.wait_for(p.stdout.readline(), 5))
    try:
        p.send_signal(signal.SIGSTOP)
        (tmp_path / "gate").touch()
        for _ in range(100):
            with Path(f"/proc/{pid}/stat").open() as f:
                if f.read().split()[2] == "Z":
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("synthetic peer did not exit")
        p.send_signal(signal.SIGCONT)
        stdout = await asyncio.wait_for(p.stdout.read(), 5)
        await asyncio.wait_for(p.wait(), 5)
        assert stdout == b"q" * 2000
    finally:
        if p.returncode is None:
            p.send_signal(signal.SIGCONT)
            p.stdin.close()
            await asyncio.wait_for(p.wait(), 5)


@pytest.mark.asyncio
async def test_broken_output_transport_cleans_owned_child(tmp_path):
    code = (
        "import os,time;print(os.getpid(),flush=True);time.sleep(.2);"
        "\nwhile True: os.write(1,b'x'*65536)"
    )
    p = await launch(code, tmp_path)
    pid = int(await asyncio.wait_for(p.stdout.readline(), 5))
    p._transport.get_pipe_transport(1).close()
    await asyncio.wait_for(p.wait(), 5)
    assert not Path(f"/proc/{pid}").exists()
    assert p.returncode != 0
    assert p._wsl_cleanup_verified is False


@pytest.mark.asyncio
async def test_system_python_310_runs_embedded_supervisor(tmp_path):
    from acp_gateway.adapters.wsl import close_wsl

    p = await launch(
        "import sys;[print(s.strip(),flush=True) for s in sys.stdin]",
        tmp_path,
        runner_python="/usr/bin/python3",
    )
    try:
        p.stdin.write("minimum runtime 雪\n".encode())
        await p.stdin.drain()
        assert await asyncio.wait_for(p.stdout.readline(), 5) == "minimum runtime 雪\n".encode()
        assert await close_wsl(p) is True
    finally:
        if p.returncode is None:
            p.stdin.close()
            await asyncio.wait_for(p.wait(), 5)


@pytest.mark.asyncio
async def test_inherited_sigchld_ignore_cannot_lose_owned_pid_identity(tmp_path):
    from acp_gateway.adapters.wsl import close_wsl

    p = await launch(
        "import time;time.sleep(.1);print('done',flush=True)",
        tmp_path,
        prelude="import signal;signal.signal(signal.SIGCHLD,signal.SIG_IGN)\n",
    )
    assert await asyncio.wait_for(p.stdout.read(), 5) == b"done\n"
    assert await close_wsl(p) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exited", "stdin-eof"])
async def test_final_drain_deadline_never_verifies_dropped_bytes(tmp_path, ending):
    """A dead child or explicit EOF must not bypass final-drain accounting."""
    size = 800000 if ending == "exited" else 400000
    code = f"import os,time;os.write(1,b'A'*{size})"
    if ending == "stdin-eof":
        code += ";time.sleep(30)"
    p = await launch(code, tmp_path, limit=262144 if ending == "exited" else 65536)
    try:
        if ending == "stdin-eof":
            await asyncio.sleep(0.5)
            p.stdin.close()
        await asyncio.sleep(4)
        stdout, stderr = await asyncio.wait_for(p.communicate(), 8)
        assert len(stdout) < size, "regression requires actual final-drain truncation"
        assert p.returncode != 0, f"silently dropped {size - len(stdout)} output bytes"
        assert p._wsl_cleanup_verified is False
        assert b"ACPGW_WSL_CLEAN" not in stderr
    finally:
        if p.returncode is None:
            p.kill()
            await p.communicate()


@pytest.mark.asyncio
async def test_supervisor_capability_is_private_from_native_proc_inspection(tmp_path):
    code = (
        "import os,json,re\n"
        "parent='/proc/'+str(os.getppid())\n"
        "result={'blocked':[], 'env_key': 'ACPGW_WSL_CLEANUP_TOKEN' in os.environ}\n"
        "for name in ['environ','fd/2','mem']:\n"
        " try:\n"
        "  fd=os.open(parent+'/'+name,os.O_RDONLY);os.close(fd)\n"
        " except PermissionError: result['blocked'].append(name)\n"
        "with open(parent+'/cmdline','rb') as f: cmdline=f.read()\n"
        "result['argv_has_capability']=bool(re.search(rb'[0-9a-f]{32}',cmdline))\n"
        "print(json.dumps(result),flush=True)\n"
        "input()\n"
    )
    p = await launch(code, tmp_path)
    try:
        report = json.loads(await asyncio.wait_for(p.stdout.readline(), 5))
        assert report == {
            "blocked": ["environ", "fd/2", "mem"],
            "env_key": False,
            "argv_has_capability": False,
        }
        p.stdin.close()
        await asyncio.wait_for(p.communicate(), 5)
        assert p.returncode == 0
        assert p._wsl_cleanup_verified is True
    finally:
        if p.returncode is None:
            p.kill()
            await p.communicate()


@pytest.mark.asyncio
async def test_begin_is_random_and_precedes_native_stderr(tmp_path):
    import re

    tokens = []
    for _ in range(2):
        p = await launch(
            "import os;os.write(2,b'native diagnostic\\n');print('ready',flush=True);input()",
            tmp_path,
            watch=False,
        )
        try:
            assert await asyncio.wait_for(p.stdout.readline(), 5) == b"ready\n"
            _, stderr = await asyncio.wait_for(p.communicate(), 5)
            match = re.fullmatch(
                rb"\x1eACPGW_WSL_BEGIN:([0-9a-f]{32})\nnative diagnostic\n"
                rb"\x1eACPGW_WSL_CLEAN:\1\n",
                stderr,
            )
            assert match is not None
            tokens.append(match[1])
            assert p.returncode == 0
        finally:
            if p.returncode is None:
                p.kill()
                await p.communicate()
    assert tokens[0] != tokens[1]


@pytest.mark.asyncio
async def test_dumpable_protection_unavailable_fails_before_handshake_or_native_spawn(tmp_path):
    prelude = (
        "import ctypes\n"
        "_real_cdll=ctypes.CDLL\n"
        "class Denied:\n"
        " def __init__(self,*a,**kw): self.lib=_real_cdll(*a,**kw)\n"
        " def prctl(self,op,*args): return -1 if op==4 else self.lib.prctl(op,*args)\n"
        "ctypes.CDLL=Denied\n"
    )
    p = await launch("print('UNSAFE_CHILD',flush=True)", tmp_path, prelude=prelude, watch=False)
    stdout, stderr = await asyncio.wait_for(p.communicate(), 5)
    assert p.returncode == 125
    assert stdout == b""
    assert stderr == b"WSL subprocess failed\n"


@pytest.mark.asyncio
async def test_drained_output_larger_than_queue_closes_cleanly_on_eof(tmp_path):
    from acp_gateway.adapters.wsl import close_wsl

    p = await launch(
        "import os;os.write(1,b'A'*800000);print('ready',flush=True);input()", tmp_path
    )
    try:
        assert await asyncio.wait_for(p.stdout.readexactly(800006), 5) == b"A" * 800000 + b"ready\n"
        assert await close_wsl(p) is True
        assert p.returncode == 0
    finally:
        if p.returncode is None:
            p.kill()
            await p.communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["short", "broken"])
async def test_failed_receipt_write_never_exits_zero(tmp_path, failure):
    prelude = (
        "import os\n"
        "_write=os.write\n"
        "def fail_receipt(fd,data):\n"
        " if fd==2 and data.startswith(b'\\x1eACPGW_WSL_CLEAN:'):\n"
        + (
            "  return _write(fd,data[:20])\n"
            if failure == "short"
            else "  raise BrokenPipeError()\n"
        )
        + " return _write(fd,data)\n"
        "os.write=fail_receipt\n"
    )
    p = await launch("print('ready',flush=True);input()", tmp_path, prelude=prelude)
    try:
        assert await asyncio.wait_for(p.stdout.readline(), 5) == b"ready\n"
        await asyncio.wait_for(p.communicate(), 5)
        assert p.returncode == 126
        assert p._wsl_cleanup_verified is False
    finally:
        if p.returncode is None:
            p.kill()
            await p.communicate()


@pytest.mark.asyncio
async def test_cancelled_output_readers_do_not_hang_close_or_verify_truncation(tmp_path):
    from acp_gateway.adapters.wsl import close_wsl

    p = await launch(
        "import os;print('ready',flush=True);input()\nwhile True: os.write(1,b'A'*65536)",
        tmp_path,
    )
    try:
        assert await asyncio.wait_for(p.stdout.readline(), 5) == b"ready\n"
        readers = [asyncio.create_task(stream.read()) for stream in (p.stdout, p.stderr)]
        await asyncio.sleep(0)
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        p.stdin.write(b"go\n")
        await p.stdin.drain()
        await asyncio.sleep(0.5)
        assert await asyncio.wait_for(close_wsl(p), 7) is False
        assert p.returncode != 0
        assert p._wsl_cleanup_verified is False
        assert p._transport.is_closing()
    finally:
        if p.returncode is None:
            p.kill()
            await p.communicate()
