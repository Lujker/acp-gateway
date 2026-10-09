"""Opt-in Docker/IP/nginx/L4-mirror smoke using two recorded ACP mocks.

Run after building the image: uv run python scripts/smoke_docker.py --image acpgw:local
All containers, networks, ports, config, DB and credentials are disposable.
No production services, Telegram API, local configuration files are used.
"""

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from acp_gateway.agents import AgentClient
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.control import ConnectorAccessError, connect_control
from acp_gateway.connectors.policy import LocalAgentPolicy, PolicyTransport
from acp_gateway.connectors.protocol import AgentManifest, Hello
from acp_gateway.connectors.relay import RelayPeer
from fakes.certs import fingerprint, make_cert
from fakes.fake_goose import FakeGooseServer

REPO = Path(__file__).resolve().parents[1]


def run(*args, allowed=(0,)):
    # All commands are controlled argv arrays; no shell or production resource names.
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)  # noqa: S603
    if result.returncode not in allowed:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def smoke(image, deployment=None):
    deployment = deployment or REPO / "deploy/docker"
    name = "acpgw-smoke-" + secrets.token_hex(4)
    network = name + "-proxy"
    origin, mirror = name + "-origin", name + "-mirror"
    with tempfile.TemporaryDirectory(prefix=name) as directory, contextlib.ExitStack() as stack:
        root = Path(directory)
        for part in ("runtime", "state", "state/enrollment"):
            (root / part).mkdir(mode=0o700, parents=True, exist_ok=True)
        settings = yaml.safe_load((deployment / "gateway.example.yaml").read_text())
        for agent in settings["agents"]:
            agent["default_cwd"] = "/work"
        (root / "runtime/gateway.yaml").write_text(yaml.safe_dump(settings))
        (root / "runtime/gateway.env").write_text(f"ACPGW_API_TOKEN={secrets.token_urlsafe(32)}\n")
        (root / "runtime/gateway.env").chmod(0o600)
        (root / ".env").write_text(f"ACPGW_UID={os.getuid()}\nACPGW_GID={os.getgid()}\n")
        (root / "override.yaml").write_text(
            yaml.safe_dump(
                {
                    "networks": {"proxy": {"external": True, "name": network}},
                    "services": {
                        "gateway": {
                            "image": image,
                            "ports": ["127.0.0.1::8766"],
                        }
                    },
                }
            )
        )
        compose = [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(root / ".env"),
            "-p",
            name,
            "-f",
            str(deployment / "compose.yaml"),
            "-f",
            str(root / "override.yaml"),
        ]
        cli = [
            "exec",
            "-T",
            "gateway",
            "acpgw",
            "--config",
            "/config/gateway.yaml",
            "--env-file",
            "/config/gateway.env",
        ]

        async def command(*args, decode=False, allowed=(0,)):
            output = await asyncio.to_thread(run, *compose, *cli, *args, allowed=allowed)
            return json.loads(output) if decode else output

        connections, tasks, mocks, profiles = [], [], {}, {}
        run("docker", "network", "create", network)
        try:
            run(*compose, "up", "-d", "--no-build")
            async with asyncio.timeout(30):
                while True:
                    try:
                        await command("status")
                        break
                    except RuntimeError:
                        await asyncio.sleep(0.2)
            direct_port = run(*compose, "port", "gateway", "8766").rsplit(":", 1)[1]
            for computer in ("home", "work"):
                await command(
                    "computers",
                    "enroll",
                    computer,
                    "--name",
                    computer,
                    "--token-file",
                    f"/data/enrollment/{computer}.key",
                )
                secret = secrets.token_urlsafe(32)
                mocks[computer] = stack.enter_context(FakeGooseServer(secret))
                profile = AgentProfile(
                    alias="goose",
                    kind="goose",
                    url=mocks[computer].url("ws"),
                    default_cwd="/work",
                    session_mode="approve",
                )
                profiles[computer] = AgentClient(
                    profile, SecretStr(secret), pin_dir=root / computer
                )

            async def connect(computer, url, pin=None):
                local = profiles[computer]

                async def factory(alias):
                    require(alias == "goose", "unexpected local alias")
                    transport, _ = await local.open_transport()
                    return PolicyTransport(transport, LocalAgentPolicy.from_profile(local.profile))

                try:
                    connection, welcome = await connect_control(
                        url,
                        credential=(root / f"state/enrollment/{computer}.key").read_text().strip(),
                        hello=Hello(
                            computer_id=computer,
                            agents=[AgentManifest(alias="goose", display_name="Goose")],
                        ),
                        fingerprint=pin,
                    )
                except ConnectorAccessError as exc:
                    cause = exc.__context__
                    if isinstance(cause, ssl.SSLCertVerificationError):
                        print(
                            f"TLS verify_code={cause.verify_code}: {cause.verify_message}",
                            flush=True,
                        )
                        print(
                            f"Test clock: {time.time()}, certificate: "
                            f"{ssl._ssl._test_decode_cert(str(root / 'cert-False.pem'))}",
                            flush=True,
                        )
                    raise
                peer = RelayPeer(
                    connection, welcome.connection_id, ["goose"], local_factory=factory
                )
                connections.append(connection)
                tasks.append(asyncio.create_task(peer.run()))
                await peer.ready.wait()

            async def ask(computer, text):
                job = await command(
                    "ask", "--agent", computer + "/goose", text, "--no-stream", decode=True
                )
                result = await command(
                    "result", job["id"], "--wait", "10", "--json", decode=True, allowed=(0, 1)
                )
                require(result["status"] == "completed", f"job failed: {result['status']}")
                return result["answer"]

            direct = f"ws://127.0.0.1:{direct_port}/acpgw/connect"
            for computer in ("home", "work"):
                await connect(computer, direct)
                require(await ask(computer, "code word " + computer) == "OK", "memory setup failed")
            for computer in ("home", "work"):
                require(await ask(computer, "What was the code word") == computer, "memory leaked")
            require(
                await ask("work", "run exactly: echo hi .") == "DENIED", "no-human action allowed"
            )
            print(
                "PASS: Docker + direct IP/port, two routes, isolated memory and no-human deny.",
                flush=True,
            )
            for connection in connections:
                await connection.close()
            await asyncio.gather(*tasks)
            connections.clear()
            tasks.clear()

            cert, _key = make_cert(root, hostname="gateway.example.com")
            snippet = (deployment / "nginx-location.conf.example").read_text()
            origin_config = (deployment / "nginx.conf.example").read_text()
            origin_config = (
                origin_config.replace(
                    "listen 443 ssl;", "listen 443 ssl; listen 17443 ssl proxy_protocol;"
                )
                .replace("/etc/nginx/tls/fullchain.pem", "/certs/cert-False.pem")
                .replace("/etc/nginx/tls/privkey.pem", "/certs/key-False.pem")
                .replace("include /etc/nginx/acpgw-location.conf;", snippet)
            )
            (root / "origin.conf").write_text(origin_config)
            (root / "mirror.conf").write_text(
                "events {}\n"
                "stream { server { listen 443; ssl_preread on; "
                f"proxy_pass {origin}:17443; proxy_protocol on; proxy_timeout 120s; }} }}\n"
            )
            for container, config in ((origin, "origin.conf"), (mirror, "mirror.conf")):
                run(
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--network",
                    network,
                    "-p",
                    "127.0.0.1::443",
                    "-v",
                    f"{root / config}:/etc/nginx/nginx.conf:ro",
                    "-v",
                    f"{root}:/certs:ro",
                    "nginx:alpine",
                )
                try:
                    run("docker", "exec", container, "nginx", "-t")
                except RuntimeError as exc:
                    raise RuntimeError(run("docker", "logs", container)) from exc
            mirror_port = run("docker", "port", mirror, "443/tcp").rsplit(":", 1)[1]
            original = socket.getaddrinfo

            def local_domain(host, *args, **kwargs):
                return original(
                    "127.0.0.1" if host == "gateway.example.com" else host, *args, **kwargs
                )

            with patch("socket.getaddrinfo", local_domain):
                url = f"wss://gateway.example.com:{mirror_port}/acpgw/connect"
                for computer in ("home", "work"):
                    await connect(computer, url, fingerprint(cert))
                    require(
                        await ask(computer, "What was the code word") == computer, "load failed"
                    )
                try:
                    await connect_control(
                        url,
                        credential="wrong",
                        hello=Hello(
                            computer_id="work",
                            agents=[AgentManifest(alias="goose", display_name="Goose")],
                        ),
                        fingerprint=fingerprint(cert),
                    )
                except ConnectorAccessError:
                    pass
                else:
                    raise AssertionError("invalid computer credential accepted")
                await connections[1].close()
                await tasks[1]
                require(await ask("home", "say pong") == "pong", "sibling failed after disconnect")
                await connect("work", url, fingerprint(cert))
                require(
                    await ask("work", "What was the code word") == "work", "reconnect lost session"
                )
            print(
                "PASS: WSS + nginx path + L4/PROXY mirror, auth, disconnect and session load.",
                flush=True,
            )
            inspection = json.loads(run("docker", "inspect", run(*compose, "ps", "-q", "gateway")))[
                0
            ]
            require(inspection["HostConfig"]["ReadonlyRootfs"], "writable root filesystem")
            require(inspection["Config"]["User"] == f"{os.getuid()}:{os.getgid()}", "wrong UID/GID")
            require(set(inspection["NetworkSettings"]["Ports"]) == {"8766/tcp"}, "unexpected ports")
            print(
                "PASS: read-only runtime, configured UID/GID, owner API has no published port.",
                flush=True,
            )
        finally:
            for connection in connections:
                await connection.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for container in (mirror, origin):
                with contextlib.suppress(RuntimeError):
                    run("docker", "rm", "-f", container)
            run(*compose, "down", "--remove-orphans")
            run("docker", "network", "rm", network)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="acpgw:local")
    parser.add_argument("--deployment", type=Path, help="test an extracted release deploy/docker")
    args = parser.parse_args()
    started = time.monotonic()
    asyncio.run(smoke(args.image, args.deployment.resolve() if args.deployment else None))
    print(f"Docker smoke completed in {time.monotonic() - started:.1f}s.")
