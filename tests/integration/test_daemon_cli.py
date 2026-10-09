"""Real HTTP/SSE and CLI processes against the recorded Goose mock."""

import asyncio
import json
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
import yaml

from acp_gateway.cli.client import events
from acp_gateway.config import load_config
from acp_gateway.daemon import configured_app
from acp_gateway.storage import Store
from fakes.fake_goose import FakeGooseServer

OWNER = "mock-owner-credential"
SECRET = "mock-goose-" + "secret-7781"
MCP = "mock-mcp-credential"


@contextmanager
def daemon(config, sock):
    ready = threading.Event()
    state = {}

    def run():
        try:
            with configured_app(config) as app:
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        log_level="warning",
                        access_log=False,
                        timeout_graceful_shutdown=1,
                    )
                )
                state["server"] = server
                ready.set()

                async def serve():
                    state["loop"] = asyncio.get_running_loop()
                    await server.serve(sockets=[sock])

                asyncio.run(serve())
        except BaseException as exc:
            state["error"] = exc
            ready.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert ready.wait(5), "daemon construction timed out"
        for _ in range(250):
            if "error" in state:
                raise state["error"]
            if state["server"].started:
                break
            threading.Event().wait(0.02)
        else:
            pytest.fail("daemon startup timed out")
        yield
    finally:
        if "server" in state:
            loop = state.get("loop")
            if loop is not None and not loop.is_closed():
                # Wake the server's selector as well as setting its exit flag;
                # the owner thread must not rely on a pending timer to wake it.
                loop.call_soon_threadsafe(setattr, state["server"], "should_exit", True)
        # Shutdown includes HTTP draining plus the WebSocket close handshake
        # (whose default timeout alone is 10 seconds).
        # A timed join has returned early on the WSL test host. Enforce the
        # existing 20-second budget using a monotonic deadline, including when
        # the underlying timed lock wakes before the requested interval.
        deadline = time.monotonic() + 20
        while thread.is_alive() and (remaining := deadline - time.monotonic()) > 0:
            thread.join(min(remaining, 0.1))
        if thread.is_alive():
            frame = sys._current_frames().get(thread.ident)
            if frame is not None:
                traceback.print_stack(frame)
            for task in asyncio.all_tasks(state["loop"]):
                task.print_stack()
        assert not thread.is_alive(), "daemon shutdown timed out"
        # Uvicorn owns this socket until its thread has finished shutdown.
        sock.close()
        if "error" in state:
            raise state["error"]


@pytest.fixture
def running(tmp_path):
    with FakeGooseServer(SECRET) as goose:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "gateway": {"port": port},
                    "data_dir": str(tmp_path / "data"),
                    "agents": [
                        {
                            "alias": "work",
                            "kind": "goose",
                            "url": goose.url("ws"),
                            "secret_env": "AGENT_WORK_SECRET",
                            "default_cwd": "/work",
                        }
                    ],
                }
            )
        )
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"ACPGW_API_TOKEN={OWNER}\nACPGW_MCP_TOKEN={MCP}\nAGENT_WORK_SECRET={SECRET}\n"
        )
        cfg = load_config(path, env_file)
        with (
            daemon(cfg, sock),
            httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": f"Bearer {OWNER}"},
                trust_env=False,
                timeout=10,
            ) as client,
        ):
            yield client, path, env_file, goose


def cli(running, *args, check=True):
    _, path, env_file, _ = running
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "acp_gateway",
            "--config",
            str(path),
            "--env-file",
            str(env_file),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if check:
        assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert OWNER not in result.stdout + result.stderr
    return result


def test_cli_multi_turn_sessions_and_targeted_prompt(running):
    status = json.loads(cli(running, "status").stdout)
    assert status["gateway"]["status"] == "running"
    assert not status["channels"][0]["connected"]
    assert cli(running, "ask", "remember code word peach").stdout.strip() == "OK"
    assert cli(running, "ask", "What was the code word").stdout.strip() == "peach"
    sessions = json.loads(cli(running, "sessions").stdout)
    old_id = sessions["active_session_id"]
    new = json.loads(cli(running, "new").stdout)
    assert new["id"] != old_id
    # Targeting an old session leaves the active pointer on the new one.
    assert (
        cli(running, "ask", "What was the code word", "--session", str(old_id)).stdout.strip()
        == "peach"
    )
    assert json.loads(cli(running, "sessions").stdout)["active_session_id"] == new["id"]
    assert cli(running, "ask", "What was the code word").stdout.strip() == "unknown"
    cli(running, "switch", str(old_id))
    job = json.loads(cli(running, "ask", "say pong", "--no-stream").stdout)
    final = json.loads(cli(running, "result", job["id"], "--wait", "5", "--json").stdout)
    assert final["answer"] == "pong" and final["status"] == "completed"
    forbidden = cli(
        running, "ask", "pong", "--thread", "other", "--session", str(old_id), check=False
    )
    assert forbidden.returncode == 1 and "HTTP 404" in forbidden.stderr


@pytest.mark.parametrize("action,answer", [("approve", "hi"), ("reject", "DENIED")])
def test_cli_decision_requires_live_watcher_and_persists_audit(running, action, answer):
    client, path, _, _ = running
    with client.stream("GET", "/approvals/events") as response:
        iterator = events(response)
        kind, connected = next(iterator)
        assert kind == "connected"
        lease = connected["lease_id"]
        job = client.post("/messages", json={"text": "run exactly: echo hi ."}).json()
        kind, data = next(iterator)
        assert kind == "ApprovalRequested"
        approval = data["approval"]
        bad = client.post(
            f"/approvals/{approval['id']}",
            json={
                "lease_id": "forged",
                "option_id": "allow_once",
            },
        )
        assert bad.status_code == 403
        cli(running, "approvals", action, approval["id"])
        final = client.get(f"/jobs/{job['id']}", params={"wait": 5}).json()
        assert final["answer"] == answer
        stale = client.post(
            f"/approvals/{approval['id']}",
            json={
                "lease_id": lease,
                "option_id": "allow_once",
            },
        )
        assert stale.status_code == 404
    # Wait for the server to observe the disconnect, without assuming socket close is synchronous.
    for _ in range(100):
        if not client.get("/health").json()["channels"][0]["connected"]:
            break
        threading.Event().wait(0.02)
    else:
        pytest.fail("disconnected approval watcher kept its human lease")
    denied = client.post("/messages", json={"text": "run exactly: echo hi .", "wait": 5}).json()
    assert denied["answer"] == "DENIED"
    assert denied["error"] == "no human approver is connected"
    store = Store.open(path.parent / "data" / "gateway.db")
    try:
        (audit,) = store.approval_audit(job["id"])
        assert audit.actor == "local-api-owner" and audit.decided_channel == "cli"
    finally:
        store.close()


def test_api_cancel_pending_approval_and_completed_sse(running):
    client, _, _, _ = running
    with client.stream("GET", "/approvals/events") as response:
        iterator = events(response)
        next(iterator)
        job = client.post("/messages", json={"text": "run exactly: sleep 30 ."}).json()
        assert next(iterator)[0] == "ApprovalRequested"
        cancelled = client.post(f"/jobs/{job['id']}/cancel").json()
        assert cancelled["status"] == "cancelled"
        assert client.get("/approvals").json() == {"approvals": []}
    with client.stream("GET", f"/jobs/{job['id']}/events") as response:
        frames = list(events(response))
    assert len(frames) == 1 and frames[0][0] == "snapshot"
    assert frames[0][1]["status"] == "cancelled"


def test_interactive_cli_watcher_rejects_and_disconnects(running, tmp_path):
    client, path, env_file, _ = running
    output = tmp_path / "watch-output.txt"
    with output.open("w") as capture:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "acp_gateway",
                "--config",
                str(path),
                "--env-file",
                str(env_file),
                "approvals",
                "watch",
            ],
            stdin=subprocess.PIPE,
            stdout=capture,
            stderr=capture,
            text=True,
        )
        try:
            for _ in range(200):
                if "Watching approvals" in output.read_text():
                    break
                assert process.poll() is None, output.read_text()
                threading.Event().wait(0.02)
            else:
                pytest.fail("interactive watcher did not connect")
            process.stdin.write("r\n")
            process.stdin.flush()
            response = client.post("/messages", json={"text": "run exactly: echo hi .", "wait": 5})
            assert response.json()["answer"] == "DENIED"
            assert "Decision sent." in output.read_text()
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
            process.stdin.close()
        assert process.returncode == 130


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_serve_command_binds_loopback_and_releases_lock(running, tmp_path, host):
    _, path, env_file, _ = running
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as sock:
        try:
            sock.bind((host.strip("[]"), 0))
        except OSError:
            if family == socket.AF_INET6:
                pytest.skip("IPv6 loopback is unavailable")
            raise
        port = sock.getsockname()[1]
    settings = yaml.safe_load(path.read_text())
    settings["gateway"] = {"host": host, "port": port}
    settings["data_dir"] = str(tmp_path / "other-data")
    other = tmp_path / "other-config.yaml"
    other.write_text(yaml.safe_dump(settings))
    output = tmp_path / "daemon-output.txt"
    with output.open("w") as capture:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "acp_gateway",
                "--config",
                str(other),
                "--env-file",
                str(env_file),
                "serve",
            ],
            stdout=capture,
            stderr=capture,
        )
        try:
            with httpx.Client(
                base_url=f"http://{host}:{port}",
                trust_env=False,
                headers={"Authorization": f"Bearer {OWNER}"},
                timeout=1,
            ) as c:
                for _ in range(200):
                    assert process.poll() is None, output.read_text()
                    try:
                        if c.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    threading.Event().wait(0.02)
                else:
                    pytest.fail("serve command did not start")
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
        assert process.returncode in {0, 130}, output.read_text()
    with configured_app(load_config(other, env_file)):
        pass
