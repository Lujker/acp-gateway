"""Fixed helper entries bypass config loading and preserve the owned supervisor contract."""

import asyncio
import json
import os
import sys

import pytest

from acp_gateway.adapters.wsl import _watch_cleanup, close_wsl

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux helper entries")


async def test_internal_worker_entry_is_noninteractive_and_does_not_need_gateway_config(tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "acp_gateway",
        "__shell-worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path,
        env={"LANG": "C.UTF-8", "PATH": os.defpath},
    )
    packet = json.dumps(
        {"argv": ["/bin/sh", "-c", "printf helper"], "cwd": str(tmp_path), "output_limit": 60_000}
    )
    stdout, stderr = await asyncio.wait_for(process.communicate((packet + "\n").encode()), 10)
    assert process.returncode == 0 and not stderr
    result = json.loads(stdout)
    assert result["exit_code"] == 0 and result["stdout"] == "helper"


async def test_internal_supervisor_entry_does_not_prefetch_worker_input(tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "acp_gateway",
        "__shell-supervisor",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path,
        env={"LANG": "C.UTF-8", "PATH": os.defpath},
        limit=1_048_576,
    )
    _watch_cleanup(process)
    drain = asyncio.create_task(process.stderr.read())
    try:
        launch = {
            "argv": [sys.executable, "-m", "acp_gateway", "__shell-worker"],
            "cwd": str(tmp_path),
            "agent_home_env": "CODEX_HOME",
            "agent_home": None,
            "home": None,
            "path": [],
        }
        packet = {
            "argv": ["/bin/sh", "-c", "printf 'supervised\\n'"],
            "cwd": str(tmp_path),
            "output_limit": 60_000,
        }
        process.stdin.write((json.dumps(launch) + "\n" + json.dumps(packet) + "\n").encode())
        await process.stdin.drain()
        response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert response["exit_code"] == 0 and "supervised" in response["stdout"]
        assert await close_wsl(process)
        assert not await drain
    finally:
        if process.returncode is None:
            await close_wsl(process)
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
