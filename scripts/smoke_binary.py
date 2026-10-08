"""Exercise a built executable outside the checkout and without Python on PATH."""

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from dotenv import dotenv_values
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from acp_gateway.cli.client import events

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from fakes.certs import fingerprint, make_cert
from fakes.fake_goose import FakeGooseServer


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument(
        "--container-image", help="run every binary command in a clean Docker image"
    )
    args = parser.parse_args()
    binary = args.binary.resolve()
    with tempfile.TemporaryDirectory(prefix="acpgw-binary-") as directory:
        root = Path(directory)
        empty_bin = root / "bin"
        empty_bin.mkdir()
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("ACPGW_", "AGENT_", "PYTHON"))
        }
        env.update(
            {
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "PATH": str(empty_bin),
            }
        )

        launcher = []
        if args.container_image:
            docker = shutil.which("docker")
            require(docker is not None, "Docker is required for --container-image")
            launcher = [
                docker,
                "run",
                "--rm",
                "--interactive",
                "--network",
                "host",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--workdir",
                str(root),
                "--volume",
                f"{root}:{root}",
                "--volume",
                f"{binary}:{binary}:ro",
            ]
            for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "PATH"):
                launcher += ["--env", f"{name}={env[name]}"]
            launcher += [args.container_image]

        def run(*arguments):
            result = subprocess.run(  # noqa: S603 (explicit binary, no shell)
                [*launcher, str(binary), *arguments],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            require(result.returncode == 0, "binary command failed: " + " ".join(arguments))
            return result.stdout

        require("0.0.0" not in run("--version"), "package version metadata was not bundled")
        for command in (
            (),
            ("config",),
            ("service",),
            ("computers",),
            ("approvals",),
            ("ask",),
            ("result",),
        ):
            require("usage:" in run(*command, "--help"), "missing command help")
        run("paths")
        run("setup")
        config_dir = root / "config/acp-gateway"
        config = config_dir / "config.yaml"
        env_file = config_dir / ".env"
        original = env_file.read_bytes()
        run("setup")
        require(env_file.read_bytes() == original, "setup replaced existing tokens")
        first_key, next_key = root / "computer.key", root / "computer-next.key"
        output = run(
            "computers",
            "enroll",
            "smoke",
            "--name",
            "Smoke computer",
            "--token-file",
            str(first_key),
        )
        credential = first_key.read_text().strip()
        require(credential not in output, "computer credential was printed")
        require(first_key.stat().st_mode & 0o077 == 0, "computer credential file is public")
        enrolled = json.loads(run("computers", "list"))
        require(len(enrolled) == 1 and enrolled[0]["generation"] == 1, "computer not enrolled")
        run("computers", "rotate", "smoke", "--token-file", str(next_key))
        require(next_key.read_text().strip() != credential, "computer credential not rotated")
        run("computers", "revoke", "smoke")
        revoked = json.loads(run("computers", "list"))
        require(
            not revoked[0]["enabled"] and revoked[0]["generation"] == 2,
            "computer revocation failed",
        )
        tokens = dotenv_values(env_file)
        cert, key = make_cert(root)
        with FakeGooseServer("mock-binary-agent-credential", certfile=cert, keyfile=key) as goose:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            settings = yaml.safe_load(config.read_text())
            settings["gateway"]["port"] = port
            settings["agents"] = [
                {
                    "alias": "work",
                    "kind": "goose",
                    "url": goose.url("wss"),
                    "tls_fingerprint": fingerprint(cert),
                    "secret_env": "AGENT_WORK_SECRET",
                    "default_cwd": "/work",
                }
            ]
            config.write_text(yaml.safe_dump(settings))
            with env_file.open("a") as stream:
                stream.write("AGENT_WORK_SECRET=mock-binary-agent-credential\n")
            run("config", "check")

            def start():
                log = (root / ("daemon-" + secrets.token_hex(3) + ".log")).open("w")
                process = subprocess.Popen(  # noqa: S603
                    [*launcher, str(binary), "serve"],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                log.close()
                return process

            def stop(process):
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
                    raise RuntimeError("binary daemon did not shut down") from None

            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                headers={"Authorization": "Bearer " + tokens["ACPGW_API_TOKEN"]},
                timeout=10,
            ) as owner:

                def ready(process):
                    for _ in range(100):
                        require(process.poll() is None, "binary daemon exited during startup")
                        try:
                            response = owner.get("/health")
                            if response.status_code == 200:
                                require(
                                    response.json()["database"]["schema_version"] == 5,
                                    "SQL migrations were not bundled",
                                )
                                return
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    raise RuntimeError("binary daemon startup timed out")

                process = start()
                try:
                    ready(process)
                    run("status")
                    run("new")
                    require(
                        run("ask", "remember code word peach").strip() == "OK", "CLI prompt failed"
                    )
                    require(
                        run("ask", "What was the code word").strip() == "peach", "CLI memory failed"
                    )
                    session_id = json.loads(run("sessions"))["active_session_id"]
                    run("switch", str(session_id))
                    job = json.loads(run("ask", "pong", "--no-stream"))
                    final = json.loads(run("result", job["id"], "--wait", "5", "--json"))
                    require(final["answer"] == "pong", "job result failed")
                    run("stop")
                    run("approvals")
                    with owner.stream("GET", "/approvals/events") as response:
                        frames = events(response)
                        next(frames)
                        job = owner.post(
                            "/messages", json={"text": "run exactly: echo hi ."}
                        ).json()
                        kind, data = next(frames)
                        require(kind == "ApprovalRequested", "permission was not routed")
                        run("approvals", "approve", data["approval"]["id"])
                        require(
                            owner.get(f"/jobs/{job['id']}?wait=5").json()["answer"] == "hi",
                            "CLI approval failed",
                        )
                        job = owner.post(
                            "/messages", json={"text": "run exactly: sleep 30 ."}
                        ).json()
                        require(
                            next(frames)[0] == "ApprovalResolved", "missing approval settlement"
                        )
                        require(next(frames)[0] == "ApprovalRequested", "missing second permission")
                        run("stop")
                        require(
                            owner.get(f"/jobs/{job['id']}").json()["status"] == "cancelled",
                            "cancel pending permission failed",
                        )

                    watch_log = root / "watch.log"
                    with watch_log.open("w") as capture:
                        watcher = subprocess.Popen(  # noqa: S603
                            [*launcher, str(binary), "approvals", "watch"],
                            cwd=root,
                            env=env,
                            stdin=subprocess.PIPE,
                            stdout=capture,
                            stderr=capture,
                            text=True,
                        )
                        try:
                            for _ in range(100):
                                require(watcher.poll() is None, "interactive watcher exited")
                                if "Watching approvals" in watch_log.read_text():
                                    break
                                time.sleep(0.1)
                            else:
                                raise RuntimeError("interactive watcher did not connect")
                            watcher.stdin.write("r\n")
                            watcher.stdin.flush()
                            denied = owner.post(
                                "/messages",
                                json={
                                    "text": "run exactly: echo hi .",
                                    "wait": 5,
                                },
                            ).json()
                            require(denied["answer"] == "DENIED", "interactive CLI reject failed")
                        finally:
                            stop(watcher)
                            watcher.stdin.close()

                    async def mcp_smoke():
                        async with (
                            httpx.AsyncClient(
                                headers={"Authorization": "Bearer " + tokens["ACPGW_MCP_TOKEN"]},
                                trust_env=False,
                            ) as http,
                            streamable_http_client(
                                str(owner.base_url).rstrip("/") + "/mcp", http_client=http
                            ) as (read, write, _),
                            ClientSession(read, write) as session,
                        ):
                            await session.initialize()
                            require(
                                len((await session.list_tools()).tools) == 6, "MCP tools missing"
                            )
                            result = await session.call_tool(
                                "work_ask", {"text": "pong", "thread": "mcp"}
                            )
                            require(
                                not result.isError and result.structuredContent["answer"] == "pong",
                                "MCP agent prompt failed",
                            )

                    asyncio.run(mcp_smoke())
                finally:
                    stop(process)
                process = start()
                try:
                    ready(process)
                    require(
                        run("ask", "What was the code word").strip() == "peach",
                        "session did not persist across binary restart",
                    )
                finally:
                    stop(process)

            # Stand-in manager tests the frozen CLI/ExecStart without changing host services.
            manager = empty_bin / "systemctl"
            manager.write_text("#!/bin/sh\nexit 0\n")
            manager.chmod(0o755)
            run("service", "install")
            unit = (root / "config/systemd/user/acp-gateway.service").read_text()
            require(
                f'ExecStart="{binary}"' in unit and '"-m"' not in unit,
                "frozen service would invoke the embedded binary as Python",
            )
            for action in ("enable", "status", "disable", "start", "stop", "restart", "uninstall"):
                run("service", action)
            require(env_file.is_file(), "uninstall deleted configuration")
        print(
            "Binary smoke passed: help, setup, computer credential lifecycle, migrations, "
            "CLI/API/SSE, pinned TLS ACP, "
            "approvals, cancel, "
            "MCP, restart persistence and frozen service commands; no Python on child PATH."
        )


if __name__ == "__main__":
    main()
