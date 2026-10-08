"""Opt-in compatibility with an installed Hermes MCP subsystem (no model calls)."""

import os
import subprocess
from pathlib import Path

import pytest
from test_daemon_cli import MCP, cli
from test_daemon_cli import running as running

from acp_gateway.cli.client import events

PROBE = Path(__file__).parents[1] / "fakes" / "hermes_probe.py"
pytestmark = pytest.mark.skipif(
    not (os.environ.get("HERMES_TEST_REPO") and os.environ.get("HERMES_TEST_PYTHON")),
    reason="set HERMES_TEST_REPO and HERMES_TEST_PYTHON for actual Hermes compatibility",
)


def start_probe(running, tmp_path, scenario):
    owner = running[0]
    # No owner or agent credentials are passed to Hermes.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ACPGW_", "AGENT_"))}
    env.update(
        {
            "ACPGW_HERMES_REPO": os.environ["HERMES_TEST_REPO"],
            "ACPGW_HERMES_URL": str(owner.base_url).rstrip("/") + "/mcp",
            "ACPGW_HERMES_TOKEN": MCP,
            "HERMES_HOME": str(tmp_path / "isolated-hermes"),
        }
    )
    return subprocess.Popen(
        [os.environ["HERMES_TEST_PYTHON"], str(PROBE), scenario],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_probe(process):
    try:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert '"status": "passed"' in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_actual_hermes_discovery_dialogue_jobs_and_no_human(running, tmp_path):
    finish_probe(start_probe(running, tmp_path, "dialogue"))


@pytest.mark.parametrize("action,answer", [("approve", "hi"), ("reject", "DENIED")])
def test_actual_hermes_action_decided_from_cli(running, tmp_path, action, answer):
    with running[0].stream("GET", "/approvals/events") as response:
        frames = events(response)
        next(frames)
        process = start_probe(running, tmp_path, answer)
        try:
            kind, data = next(frames)
            assert kind == "ApprovalRequested"
            cli(running, "approvals", action, data["approval"]["id"])
            finish_probe(process)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
