"""Windows orchestration: actual helper completion precedes state restoration."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp_gateway import installation, windows_acl
from acp_gateway import service_update_worker as worker
from acp_gateway.maintenance_worker import lease


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(installation.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(installation, "lease_path", lambda: tmp_path / "installation.lock")
    monkeypatch.setattr(windows_acl, "check_private_paths", lambda *args, **kwargs: None)
    directory = tmp_path / "updates" / "installation"
    directory.mkdir(parents=True)
    proof = tmp_path / "coordinator.json"
    proof.write_text(json.dumps({"token": "private nonce"}))
    (directory / "service-operation.json").write_text(
        json.dumps({"coordinator_file": str(proof), "token": "private nonce"})
    )
    return tmp_path, proof


def test_pending_windows_coordinator_blocks_unrelated_mutation(guard):
    with (
        pytest.raises(ValueError, match="already pending"),
        installation._service_update_guard(SimpleNamespace()),
    ):
        pass


def test_windows_inner_proof_requires_live_coordinator_lock(guard):
    directory, proof = guard
    args = SimpleNamespace(service_coordinator_file=proof)
    with (
        lease(directory / "installation.service-update.lock", exclusive=True),
        installation._service_update_guard(args) as descriptor,
    ):
        assert descriptor is None
    with (
        pytest.raises(ValueError, match="no longer owns"),
        installation._service_update_guard(args),
    ):
        pass


def test_windows_forged_coordinator_proof_is_refused(guard):
    _, proof = guard
    proof.write_text(json.dumps({"token": "wrong"}))
    with (
        pytest.raises(ValueError, match="Invalid service coordinator proof"),
        installation._service_update_guard(SimpleNamespace(service_coordinator_file=proof)),
    ):
        pass


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    plan = {
        "entry": "acpgw.exe",
        "arguments": ["update"],
        "reservation": str(tmp_path / "reservation.json"),
        "result": str(tmp_path / "result.json"),
    }
    service = {
        "role": "connector",
        "config": "config.yaml",
        "env_file": ".env",
        "enabled": True,
        "running": True,
        "instance": "old",
    }
    current = {"installed": True, "state": 4, "enabled": True, "instance": "old"}
    calls, fail = [], set()

    def request(plan, service, action, **values):
        calls.append(action)
        if action in fail:
            raise ValueError("injected " + action)
        if action == "disable":
            current.update(state=1, enabled=False, instance=None)
        elif action == "maintenance-flags":
            current["enabled"] = values["enabled"]
        elif action == "start":
            current.update(state=4, instance="new")
        return current.copy()

    def admission(plan, service, action):
        calls.append(action)
        if action in fail:
            raise ValueError("injected " + action)
        if action == "prepare":
            Path(plan["reservation"]).write_text("private reservation")
        else:
            Path(plan["reservation"]).unlink()

    def invoke(command, **kwargs):
        if "--handoff-file" in command:
            calls.append("queue-package")
            handoff = Path(command[-1])
            handoff.write_text(
                json.dumps(
                    {
                        "python": str(tmp_path / "base.exe"),
                        "worker": str(tmp_path / "worker.py"),
                        "manifest": str(tmp_path / "package.json"),
                    }
                )
            )
        else:
            calls.append("finish-package")
            if "finish-package" in fail:
                raise ValueError("package failed and rolled back")
        return ""

    monkeypatch.setattr(worker, "windows_request", request)
    monkeypatch.setattr(worker, "admission", admission)
    monkeypatch.setattr(worker, "invoke", invoke)
    monkeypatch.setattr(
        worker,
        "cli",
        lambda *args, **kwargs: json.dumps(
            {"state": "running", "instance": current["instance"], "reserved": False}
        ),
    )
    return plan, service, current, calls, fail


@pytest.mark.parametrize(
    "enabled,running", [(True, True), (True, False), (False, True), (False, False)]
)
def test_windows_cycle_preserves_exact_flags_and_waits_for_helper(cycle, enabled, running):
    plan, service, current, calls, _ = cycle
    service.update(enabled=enabled, running=running, instance="old" if running else None)
    current.update(
        enabled=enabled, state=4 if running else (3 if enabled else 1), instance=service["instance"]
    )
    worker.execute_windows(plan, service)
    assert current["enabled"] is enabled
    assert (current["state"] == 4) is running
    assert ("prepare" in calls) is running
    assert calls.index("queue-package") < calls.index("finish-package")
    if running:
        assert calls.index("finish-package") < calls.index("start")


def test_package_failure_restores_original_task_after_verified_rollback(cycle):
    plan, service, current, calls, fail = cycle
    fail.add("finish-package")
    with pytest.raises(ValueError, match="rolled back"):
        worker.execute_windows(plan, service)
    assert current["enabled"] and current["state"] == 4
    assert calls.index("finish-package") < calls.index("start")


def test_busy_prepare_does_not_disable_task_or_replace_package(cycle):
    plan, service, current, calls, fail = cycle
    fail.add("prepare")
    with pytest.raises(ValueError, match="prepare"):
        worker.execute_windows(plan, service)
    assert calls == ["prepare"] and current["state"] == 4


def test_failed_stop_resumes_original_instance_without_duplicate_start(cycle):
    plan, service, current, calls, fail = cycle
    fail.add("disable")
    with pytest.raises(ValueError, match="disable"):
        worker.execute_windows(plan, service)
    assert "resume" in calls and "start" not in calls and "queue-package" not in calls
    assert current["instance"] == "old"
