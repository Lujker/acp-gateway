"""Check foreground dispatcher/connector binaries outside the checkout, with no Python PATH."""

import argparse
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from fakes.certs import fingerprint, make_cert


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(predicate, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError("connector smoke timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    with tempfile.TemporaryDirectory(prefix="acpgw-control-") as directory:
        root = Path(directory)
        empty_bin = root / "bin"
        empty_bin.mkdir()
        env = {
            "HOME": str(root),
            "PATH": str(empty_bin),
            "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
        }
        cert, key = make_cert(root)
        config, env_file, token_file = (
            root / "config.yaml",
            root / "test.env",
            root / "computer.key",
        )
        config.write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(root / "db"),
                    "agents": [
                        {
                            "alias": "goose",
                            "url": "ws://127.0.0.1:1",
                            "default_cwd": "/work",
                        }
                    ],
                }
            )
        )
        env_file.write_text("")
        prefix = [str(binary), "--config", str(config), "--env-file", str(env_file)]

        def run(*arguments):
            result = subprocess.run(  # noqa: S603 (explicit binary, no shell)
                [*prefix, *arguments], cwd=root, env=env, capture_output=True, text=True, timeout=30
            )
            require(result.returncode == 0, "connector administration failed")
            return result.stdout

        run("computers", "enroll", "work", "--name", "Smoke", "--token-file", str(token_file))
        credential = token_file.read_text().strip()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        processes = []
        logs = []
        try:
            for role in ("dispatcher", "connector"):
                path = root / f"{role}.log"
                path.touch(mode=0o600)
                stream = path.open("w")
                logs.append((path, stream))
                arguments = (
                    [
                        "dispatcher",
                        "--port",
                        str(port),
                        "--tls-cert",
                        str(cert),
                        "--tls-key",
                        str(key),
                    ]
                    if role == "dispatcher"
                    else [
                        "connector",
                        "--dispatcher-url",
                        f"wss://127.0.0.1:{port}/connect",
                        "--computer-id",
                        "work",
                        "--token-file",
                        str(token_file),
                        "--tls-fingerprint",
                        fingerprint(cert),
                    ]
                )
                process = subprocess.Popen(  # noqa: S603 (explicit binary, no shell)
                    [*prefix, *arguments], cwd=root, env=env, stdout=stream, stderr=stream
                )
                processes.append(process)
                marker = "dispatcher listening" if role == "dispatcher" else "computer connected"
                wait_for(
                    lambda marker=marker, path=path, process=process: (
                        marker in path.read_text() or process.poll() is not None
                    )
                )
                require(process.poll() is None, "foreground registration mode failed to start")
            # Survive the first native keepalive interval (20s by default).
            deadline = time.monotonic() + 22
            while time.monotonic() < deadline:
                require(
                    all(p.poll() is None for p in processes), "heartbeat disconnected the connector"
                )
                time.sleep(0.1)
            run("computers", "revoke", "work")
            processes[1].wait(timeout=10)
            require(
                processes[1].returncode == 78, "revoked connector did not stop with config error"
            )
            require(processes[0].poll() is None, "revocation stopped the dispatcher")
            for path, _ in logs:
                require(credential not in path.read_text(), "computer credential leaked into logs")
            processes[0].send_signal(signal.SIGINT)
            processes[0].wait(timeout=10)
            require(processes[0].returncode == 130, "dispatcher did not stop cleanly on interrupt")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for _, stream in logs:
                stream.close()
        print(
            "Connector binary smoke passed: pinned WSS, registration, heartbeats, live revocation, "
            "private credential and interrupt shutdown; no Python on child PATH."
        )


if __name__ == "__main__":
    main()
