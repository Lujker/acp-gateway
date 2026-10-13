"""Opt-in installed-source compatibility; synthetic state, no model or tools."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_SEMANTICS_REPO"),
    reason="set HERMES_SEMANTICS_REPO for installed Hermes handler compatibility",
)


def test_installed_handlers_match_recorded_busy_cancel_and_approval_contract(tmp_path):
    output = tmp_path / "semantics.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/spike_hermes_semantics.py"),
            "--hermes-repo",
            os.environ["HERMES_SEMANTICS_REPO"],
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(output.read_text())
    expected = json.loads((ROOT / "tests/fixtures/hermes/0.21.6/semantics.json").read_text())
    # Hashes remain in the report for diagnosis; harmless source edits alone
    # must not fail compatibility when the observed contract stays identical.
    actual.pop("source_sha256")
    expected.pop("source_sha256")
    assert actual == expected
