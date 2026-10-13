"""Disposable three-host CLI laboratory: dispatcher + home/work ACP executors.

uv run python scripts/lab.py run  # build, test and remove containers/volumes
uv run python scripts/lab.py up   # leave a lab for interactive exploration
uv run python scripts/lab.py test # test that lab
uv run python scripts/lab.py down # remove it, including private enrollment keys
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = ["acpgw", "--config", "/lab/config/config.yaml", "--env-file", "/lab/config/.env"]


def command(argv, *, timeout=60, env=None, allowed=(0,)):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)  # noqa: S603
    if result.returncode not in allowed:
        raise RuntimeError(f"Lab command failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def verify(compose, env):
    def node(identity, *args, as_json=False, allowed=(0,)):
        output = command([*compose, "exec", "-T", identity, *CLI, *args], env=env, allowed=allowed)
        return json.loads(output) if as_json else output

    deadline = time.monotonic() + 45
    while True:
        state = node("dispatcher", "computers", "status", as_json=True)
        if len(state["computers"]) == 2 and all(c["connected"] for c in state["computers"]):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Both lab computers did not register in time")
        time.sleep(0.5)
    for identity in ("dispatcher", "home", "work"):
        node(identity, "config", "check")
        report = node(identity, "doctor", "--live", "--json", as_json=True)
        if not report["ok"]:
            raise RuntimeError(f"doctor failed on {identity}: {report['checks']}")
    print("PASS: real CLI wizard/config/doctor on dispatcher + two enrolled computers", flush=True)
    command([*compose, "exec", "-T", "home", "python", "/app/lab_node.py", "agents"], env=env)
    print("PASS: targeted profile editing in a real terminal keeps private credentials", flush=True)
    command([*compose, "exec", "-T", "home", "python", "/app/lab_node.py", "negative"], env=env)
    print("PASS: rejected ACP key and missing enrollment diagnostics", flush=True)

    def ask(identity, text):
        job = node(
            "dispatcher", "ask", "--agent", identity + "/goose", text, "--no-stream", as_json=True
        )
        result = node(
            "dispatcher",
            "result",
            job["id"],
            "--wait",
            "10",
            "--json",
            as_json=True,
            allowed=(0, 1),
        )
        if result["status"] != "completed":
            raise RuntimeError("Lab ACP job did not complete: " + json.dumps(result))
        return result["answer"]

    for identity in ("home", "work"):
        if ask(identity, "code word " + identity) != "OK":
            raise RuntimeError("Lab memory setup failed")
    for identity in ("home", "work"):
        if ask(identity, "What was the code word") != identity:
            raise RuntimeError("Computer session isolation failed")
    if ask("home", "run exactly: echo hi .") != "DENIED":
        raise RuntimeError("Lab allowed an action without a human")
    print("PASS: routed ACP prompts, isolated sessions and deny without human approval", flush=True)
    command([*compose, "stop", "work"], env=env)
    if ask("home", "say pong") != "pong":
        raise RuntimeError("Sibling route failed after disconnect")
    # Restarting a disposable node recreates its fake agent sessions, so only check readiness.
    command([*compose, "start", "work"], env=env)
    deadline = time.monotonic() + 30
    while True:
        state = node("dispatcher", "computers", "status", as_json=True)
        if all(c["connected"] for c in state["computers"]):
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Computer did not reconnect")
        time.sleep(0.5)
    print("PASS: computer disconnect/reconnect; sibling remains available", flush=True)
    for identity in ("home", "work"):
        failed = node(identity, "doctor", "--timeout", "0", "--json", as_json=True, allowed=(1,))
        if failed["ok"]:
            raise RuntimeError("doctor accepted an invalid probe deadline")
    print("PASS: diagnostic failures produce nonzero status and valid JSON", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "test", "down", "run"), nargs="?", default="run")
    parser.add_argument("--project", help="isolated Compose name starting with acpgw-lab-")
    parser.add_argument("--image", default="acpgw:cli-lab")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument(
        "--anonymous-docker",
        action="store_true",
        help="use temporary Docker config for public images; bypass a broken credential helper",
    )
    args = parser.parse_args()
    env = dict(os.environ, ACPGW_LAB_IMAGE=args.image)
    if args.anonymous_docker:
        with tempfile.TemporaryDirectory(prefix="acpgw-lab-docker-") as directory:
            env["DOCKER_CONFIG"] = directory
            run_lab(args, env)
    else:
        run_lab(args, env)


def run_lab(args, env):
    project = args.project or (
        "acpgw-lab-" + secrets.token_hex(4) if args.action == "run" else "acpgw-lab-cli"
    )
    if not re.fullmatch(r"acpgw-lab-[a-z0-9-]+", project):
        raise ValueError(
            "Lab project must start with acpgw-lab- and use lowercase letters/digits/hyphens"
        )
    compose = ["docker", "compose", "-p", project, "-f", str(REPO / "deploy/lab/compose.yaml")]
    if args.action in {"up", "run"} and not args.no_build:
        print("Building isolated CLI lab image...", flush=True)
        command(
            [
                "docker",
                "build",
                "-f",
                str(REPO / "deploy/lab/Dockerfile"),
                "-t",
                args.image,
                str(REPO),
            ],
            timeout=600,
            env=env,
        )
    try:
        if args.action in {"up", "run"}:
            command([*compose, "up", "-d", "--wait", "--wait-timeout", "90"], env=env, timeout=120)
            print(f"Lab ready: {project}. No host ports or user configuration mounted.", flush=True)
        if args.action in {"test", "run"}:
            verify(compose, env)
        if args.action == "up":
            print(
                f"Explore: docker compose -p {project} "
                "-f deploy/lab/compose.yaml exec home "
                "acpgw --config /lab/config/config.yaml --env-file /lab/config/.env menu"
            )
            print(f"Cleanup: uv run python scripts/lab.py down --project {project}")
    finally:
        if args.action in {"down", "run"}:
            command([*compose, "down", "--volumes", "--remove-orphans"], env=env)
            print("Lab containers, network and private key volumes removed.", flush=True)


if __name__ == "__main__":
    main()
