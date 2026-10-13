"""Opt-in installed Hermes handler check without live provider/model calls."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(
    not os.environ.get("HERMES_MODELS_REPO"),
    reason="set HERMES_MODELS_REPO for installed Hermes model handler compatibility",
)
def test_installed_model_handler_scope_deferral_and_confirmation(tmp_path):
    output = tmp_path / "models.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/spike_hermes_models.py"),
            "--hermes-repo",
            os.environ["HERMES_MODELS_REPO"],
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
    actual = json.loads(output.read_text(encoding="utf-8"))
    expected = json.loads((ROOT / "tests/fixtures/hermes/0.21.6/models.json").read_text())
    actual.pop("source_sha256")
    expected.pop("source_sha256")
    assert actual == expected
