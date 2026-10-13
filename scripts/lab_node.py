"""Disposable Docker lab node. Drive real terminal setup; never call a model."""

import json
import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path("/lab/config")


def wizard(role, *, section=None):
    """Exercise the installed CLI through a real PTY, including getpass inputs."""
    CONFIG.mkdir(parents=True, exist_ok=True)
    command = ["acpgw", "--config", str(CONFIG / "config.yaml"), "--env-file", str(CONFIG / ".env")]
    command += (
        ["config", "edit", "--section", section]
        if section
        else ["setup", "--interactive", "--role", role]
    )
    master, slave = pty.openpty()
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave)  # noqa: S603
    os.close(slave)
    pending, transcript = b"", b""
    computers = iter(("home", "work"))
    more = iter(("yes", "no"))
    deadline = time.monotonic() + 30
    try:
        while process.poll() is None and time.monotonic() < deadline:
            if not select.select([master], [], [], 0.2)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            pending += chunk
            transcript += chunk
            if not pending.endswith(b": "):
                continue
            prompt = pending.decode(errors="replace").splitlines()[-1]
            answer = ""
            if prompt.startswith("Connector listen address"):
                answer = "0.0.0.0"  # noqa: S104 — isolated container network, no host ports
            elif prompt.startswith("Connector port"):
                answer = "8766"
            elif prompt.startswith("Computer ID registered"):
                answer = os.environ["LAB_COMPUTER"]
            elif prompt.startswith("Computer ID"):
                answer = next(computers)
            elif prompt.startswith("Add another agent?") and role == "server":
                answer = next(more)
            elif prompt.startswith(
                ("Allow unencrypted public", "Use this unencrypted", "Export enrollment")
            ):
                answer = "yes"
            elif prompt.startswith("Server connection URL"):
                answer = "ws://dispatcher:8766/acpgw/connect"
            elif prompt.startswith("Enrollment key file"):
                answer = "/keys/enrollment.key"
            elif prompt.startswith("Prepare a managed local Goose") or prompt.startswith(
                "Connect this computer to an external dispatcher now?"
            ):
                answer = "yes"
            elif prompt.startswith("Working directory"):
                answer = "/work"
            elif prompt.startswith("Check connections now"):
                answer = "no"
            os.write(master, (answer + "\n").encode())
            pending = b""
        if process.poll() is None:
            process.kill()
        if process.wait() != 0:
            # Do not emit the terminal transcript: future prompts may contain credentials.
            raise RuntimeError("Lab terminal wizard failed or timed out")
        print(f"PASS: real terminal wizard role={role} section={section or 'all'}", flush=True)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()


def main():
    role = os.environ["LAB_ROLE"]
    Path("/work").mkdir(exist_ok=True)
    if len(sys.argv) > 1:
        if sys.argv[1] == "negative":
            negative_checks()
            return
        before = (CONFIG / ".env").read_bytes()
        wizard(role, section=sys.argv[1])
        if (CONFIG / ".env").read_bytes() != before:
            raise RuntimeError("Repeated lab setup changed private credentials")
        return
    wizard("server" if role == "server" else "local")
    prefix = ["acpgw", "--config", str(CONFIG / "config.yaml"), "--env-file", str(CONFIG / ".env")]
    if role == "server":
        for identity in ("home", "work"):
            target = Path("/exports") / identity / "enrollment.key"
            shutil.copyfile(CONFIG / "enrollment" / f"{identity}.key", target)
            target.chmod(0o600)
        os.execvp(prefix[0], [*prefix, "start"])  # noqa: S606 — fixed lab-only command
    from acp_gateway.cli.executors import bin_dir

    binary = bin_dir() / "goose"
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile("/app/lab_goose.py", binary)
    binary.chmod(0o700)
    subprocess.run([*prefix, "agents", "provider"], check=True)  # noqa: S603 — lab-only mock
    executor = subprocess.Popen([*prefix, "agents", "serve", "goose"])  # noqa: S603
    deadline = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 3284), timeout=1):
                break
        except OSError:
            if time.monotonic() > deadline or executor.poll() is not None:
                executor.terminate()
                raise RuntimeError("Lab Goose CLI listener did not start") from None
            time.sleep(0.1)
    process = subprocess.Popen([*prefix, "start"])  # noqa: S603 — fixed lab-only argv

    def stop(*args):
        process.terminate()
        executor.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        result = process.wait()
        print(json.dumps({"connector_exit": result}), flush=True)
    finally:
        executor.terminate()
        try:
            executor.wait(timeout=10)
        except subprocess.TimeoutExpired:
            executor.kill()
            executor.wait()
    sys.exit(result)


def negative_checks():
    import yaml
    from dotenv import dotenv_values

    prefix = ["acpgw", "--config", str(CONFIG / "config.yaml"), "--env-file", str(CONFIG / ".env")]
    env_file = CONFIG / ".env"
    state_file = CONFIG / "setup.json"
    before_env, before_state = env_file.read_bytes(), state_file.read_bytes()

    def fails(*args):
        result = subprocess.run(  # noqa: S603 — fixed lab-only CLI arguments
            [*prefix, "doctor", "--json", *args], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 1 or json.loads(result.stdout)["ok"]:
            raise RuntimeError("Lab doctor did not report the expected configuration failure")

    try:
        raw = yaml.safe_load((CONFIG / "config.yaml").read_text())
        name = raw["agents"][0]["secret_env"]
        values = dotenv_values(env_file, interpolate=False)
        values[name] = "lab-intentionally-wrong-key"
        env_file.write_text(
            "\n".join(key + "=" + json.dumps(value) for key, value in values.items()) + "\n"
        )
        fails("--live", "--timeout", "1")
        env_file.write_bytes(before_env)
        state = json.loads(before_state)
        state["connection"]["token_file"] = "/keys/missing.key"  # noqa: S105 — path fixture
        state_file.write_text(json.dumps(state))
        fails()
        state_file.write_bytes(before_state)
    finally:
        env_file.write_bytes(before_env)
        state_file.write_bytes(before_state)
    print(
        "PASS: doctor detects rejected ACP keys and missing enrollment without exposing credentials"
    )


if __name__ == "__main__":
    main()
