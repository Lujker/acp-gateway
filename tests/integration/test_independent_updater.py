"""Real base-interpreter companion and fake installer, with no package/service updates."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from acp_gateway import update_runner, updater_service
from acp_gateway.update_journal import Journal


@pytest.mark.skipif(
    sys.platform != "linux", reason="POSIX executable fake; no native Windows claim"
)
@pytest.mark.parametrize("installed", [False, True])
def test_copied_companion_runs_without_venv_and_does_not_replay_after_restart(
    tmp_path, monkeypatch, installed
):
    entry = tmp_path / "fake-acpgw"
    entry.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""
import json, sys
from pathlib import Path
root = Path(__file__).parent
state = root / "version.txt"
args = sys.argv[1:]
if "version" in args:
    print(json.dumps({"version": state.read_text(), "manager": "uv", "source": "registry"}))
elif "update" in args:
    key = args[args.index("--operation-id") + 1]
    version = args[args.index("--version") + 1]
    with (root / "invocations.jsonl").open("a") as f: f.write(json.dumps(args) + "\n")
    state.write_text(version)
    result = root / "results" / (key + ".service-result.json")
    result.parent.mkdir(exist_ok=True)
    result.write_text(json.dumps({"operation_id": key, "status": "succeeded"}))
else:
    raise SystemExit(2)
"""
    )
    entry.chmod(0o700)
    (tmp_path / "version.txt").write_text("0.2.0")
    policy = {
        "version": 1,
        "backend": "uv",
        "role": "dispatcher",
        "target": "dispatcher",
        "spool_dir": str(tmp_path / "spool"),
        "result_dir": str(tmp_path / "results"),
        "entry": str(entry),
        "config": str(tmp_path / "config.yaml"),
        "env_file": str(tmp_path / "private.env"),
    }
    path = tmp_path / "updater-policy.json"
    path.write_text(json.dumps(policy))
    path.chmod(0o600)
    captured = []
    monkeypatch.setattr(update_runner.os, "execv", lambda exe, args: captured.append((exe, args)))
    if installed:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(updater_service, "_systemctl", lambda *a, **kw: "")
        assert updater_service.manage("install", path) == 0
        argv = updater_service.command(path)
        executable = argv[0]
    else:
        assert update_runner.start_updater(path) == 0
        executable, argv = captured[0]
    assert executable == sys._base_executable
    assert str(Path(argv[1]).parent) not in str(Path(sys.prefix))
    journal = Journal(tmp_path / "spool" / "operations.db")
    row = journal.create(
        actor="owner",
        role="dispatcher",
        target="dispatcher",
        version="0.3.0",
        current_version="0.2.0",
        binding="local",
        expires=time.time() + 120,
        status="queued",
    )

    def start():
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": ""},
        )

    def wait_for(predicate, process):
        deadline = time.monotonic() + 10
        while not predicate():
            assert process.poll() is None, process.communicate()
            if time.monotonic() >= deadline:
                pytest.fail("companion timed out")
            time.sleep(0.03)

    process = start()
    try:
        wait_for(lambda: journal.get(row["id"])["status"] == "succeeded", process)
    finally:
        process.terminate()
        process.communicate(timeout=5)
    assert process.returncode == 0
    assert journal.worker()["published"] == 0
    assert len((tmp_path / "invocations.jsonl").read_text().splitlines()) == 1
    journal.set_worker({"published": 0})
    second = journal.create(
        actor="owner",
        role="dispatcher",
        target="dispatcher",
        version="0.4.0",
        current_version="0.3.0",
        binding="local",
        expires=time.time() + 120,
        status="running",
    )
    process = start()
    try:
        wait_for(lambda: bool(journal.worker().get("published")), process)
        assert journal.get(second["id"])["status"] == "unknown"
        assert len((tmp_path / "invocations.jsonl").read_text().splitlines()) == 1
    finally:
        process.terminate()
        process.communicate(timeout=5)
        journal.close()
