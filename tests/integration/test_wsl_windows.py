"""Opt-in Windows->WSL tests with synthetic peers; never real model calls."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from acp_gateway.agents.client import AgentClient
from acp_gateway.config import AgentProfile

DISTRIBUTION = os.environ.get("ACPGW_TEST_WSL_DISTRIBUTION")
USER = os.environ.get("ACPGW_TEST_WSL_USER")
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not DISTRIBUTION or not USER,
    reason="explicit Windows WSL synthetic test opt-in required",
)


def run_linux(*argv):
    launcher = Path(os.environ["SYSTEMROOT"]) / "System32" / "wsl.exe"
    return subprocess.run(
        [str(launcher), "--distribution", DISTRIBUTION, "--user", USER, "--exec", *argv],
        capture_output=True,
        timeout=15,
    )


def linux_path(path):
    value = path.as_posix()
    assert len(path.drive) == 2 and path.drive[1] == ":"
    return "/mnt/" + path.drive[0].lower() + "/" + value[3:]


def create_client(tmp_path, kind, scenario):
    source = (
        Path(__file__).parents[1]
        / "fakes"
        / ("codex_peer.py" if kind == "codex" else "claude_code_peer.py")
    )
    script = tmp_path / "synthetic-peer"
    body = source.read_text().replace('SCENARIO = "happy"', f"SCENARIO = {scenario!r}")
    if kind == "claude_code":
        # Native request IDs are unique across turns, unlike the single-turn fixture.
        body = body.replace('"permission-1"', '("permission-" + str(len(history)))')
    header = (
        "#!/usr/bin/python3\nimport json,os; from pathlib import Path; "
        "Path('native-owned.json').write_text(json.dumps({'pid':os.getpid(),"
        "'start':Path('/proc/self/stat').read_text().split()[21]}))\n"
    )
    script.write_bytes((header + body).encode("utf-8"))
    cwd = linux_path(tmp_path)
    executable = linux_path(script)
    assert (
        run_linux(
            "/usr/bin/python3", "-c", "import os,sys;os.chmod(sys.argv[1],0o700)", executable
        ).returncode
        == 0
    )
    (tmp_path / "home").mkdir()
    (tmp_path / "agent-profile").mkdir()
    profile = AgentProfile(
        alias="synthetic",
        kind=kind,
        backend=kind,
        default_cwd=cwd,
        **{
            kind: {
                "executable": executable,
                "request_timeout": 10,
                "turn_timeout": 15,
                "wsl": {
                    "distribution": DISTRIBUTION,
                    "user": USER,
                    "home": cwd + "/home",
                    "agent_home": cwd + "/agent-profile",
                },
            }
        },
    )
    return AgentClient(profile, pin_dir=tmp_path / "pins", reconnect_delays=())


def owned_not_running(tmp_path):
    marker = tmp_path / "native-owned.json"
    if not marker.exists():
        return True
    identity = json.loads(marker.read_text())
    code = (
        "from pathlib import Path;import sys;p=Path('/proc')/sys.argv[1]/'stat';"
        "d=p.read_text().split() if p.exists() else [];"
        "sys.exit(0 if not d or d[21]!=sys.argv[2] or d[2]=='Z' else 1)"
    )
    return (
        run_linux(
            "/usr/bin/python3", "-c", code, str(identity["pid"]), identity["start"]
        ).returncode
        == 0
    )


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
@pytest.mark.parametrize("allow", [False, True])
async def test_windows_wsl_two_turns_stream_and_once_only_decisions(tmp_path, kind, allow):
    client = create_client(tmp_path, kind, "approval" if kind == "codex" else "permission-Bash")
    seen = []

    async def choose(request):
        seen.append(request)
        return request.option("allow_once" if allow else "reject_once").option_id

    client.set_permission_handler(choose)
    stream = None
    try:
        sid = await client.new_session()
        stream = client._transport
        first = await client.ask(sid, "hello")
        second = await client.ask(sid, "hello")
        assert first.text == ("Hello world" if kind == "codex" else "hello")
        assert second.text == ("Hello world" if kind == "codex" else "hello|hello")
        assert len(seen) == 2
    finally:
        await client.close()
    assert stream.process.returncode == 0 and stream.uncertain == 0
    assert stream.process._wsl_cleanup_verified is True
    assert owned_not_running(tmp_path)
    if kind == "codex":
        rows = [
            json.loads(line) for line in (tmp_path / "synthetic-peer.log").read_text().splitlines()
        ]
        decisions = [
            row["result"]["decision"]
            for row in rows
            if row.get("id") == "approval-1" and "method" not in row
        ]
        assert decisions == ["accept" if allow else "decline"] * 2
    else:
        rows = [
            json.loads(line)["frame"]
            for line in (tmp_path / "claude-peer.log").read_text().splitlines()
        ]
        decisions = [
            row["response"]["response"]["behavior"]
            for row in rows
            if row.get("type") == "control_response"
        ]
        assert decisions == ["allow" if allow else "deny"] * 2


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
async def test_windows_wrapper_loss_preserves_uncertainty_without_native_fallback(tmp_path, kind):
    client = create_client(tmp_path, kind, "happy")
    try:
        await client.connect()
        stream = client._transport
        stream.process.terminate()  # only this test's owned Windows launcher
        await asyncio.wait_for(stream.closed.wait(), 10)
    finally:
        await client.close()
    assert stream.uncertain >= 1
    assert stream.process.returncode is not None
    # Native children should stop; never equate this to native-turn cancellation.
    assert owned_not_running(tmp_path)
