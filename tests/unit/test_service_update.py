"""Service update state/rollback orchestration without changing host services."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_connector_service import computer as computer

from acp_gateway import installation, service, service_update
from acp_gateway import service_update_worker as worker
from acp_gateway.cli.main import _build_parser

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux service coordinator")


def test_unsupported_service_coordinator_refused_before_mutation(monkeypatch):
    monkeypatch.setattr(installation, "sys", SimpleNamespace(platform="darwin"))
    with pytest.raises(ValueError, match="Linux or Windows"):
        installation.maintenance(_build_parser().parse_args(["update", "--with-services"]))


def test_service_role_requires_explicit_managed_cycle():
    with pytest.raises(ValueError, match="requires --with-services"):
        installation.maintenance(
            _build_parser().parse_args(["update", "--service-role", "connector"])
        )


def test_coordinator_lock_blocks_parallel_mutation_but_accepts_child(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "lease_path", lambda: tmp_path / "installation.lock")
    args = SimpleNamespace(service_coordinator_fd=None)
    with installation._service_update_guard(args) as descriptor:
        with pytest.raises(ValueError, match="in use"), installation._service_update_guard(args):
            pass
        with installation._service_update_guard(
            SimpleNamespace(service_coordinator_fd=descriptor)
        ) as inherited:
            assert inherited == descriptor
        with pytest.raises(ValueError, match="in use"), installation._service_update_guard(args):
            pass
    with installation._service_update_guard(args):
        pass


def test_coordinator_rejects_unrelated_inherited_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "lease_path", lambda: tmp_path / "installation.lock")
    (tmp_path / "installation.service-update.lock").touch()
    descriptor = os.open(tmp_path / "other", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with (
            pytest.raises(ValueError, match="invalid service coordinator"),
            installation._service_update_guard(SimpleNamespace(service_coordinator_fd=descriptor)),
        ):
            pass
    finally:
        os.close(descriptor)


def test_pending_service_result_overrides_inner_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installation.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(installation, "lease_path", lambda: tmp_path / "installation.lock")
    monkeypatch.setattr(installation, "installation_info", lambda: {"manager": "uv"})
    directory = tmp_path / "updates" / "installation"
    directory.mkdir(parents=True)
    for name, status in (("inner", "succeeded"), ("outer", "running")):
        result = directory / (name + ".json")
        result.write_text(json.dumps({"status": status, "phase": name}))
        pointer = directory / (
            "last-operation.json" if name == "inner" else "service-operation.json"
        )
        pointer.write_text(json.dumps({"result": str(result)}))
    assert installation.maintenance(_build_parser().parse_args(["update", "--status"])) == 2
    assert json.loads(capsys.readouterr().out)["phase"] == "outer"


def test_inspection_requires_same_installation_and_selected_files(computer, tmp_path, monkeypatch):
    config, launch = computer
    unit = tmp_path / "connector.service"
    unit.write_text(service.render_unit(config.config_path, config.env_file, launch=launch))
    monkeypatch.setattr(service, "unit_path", lambda role: unit)
    monkeypatch.setattr(
        service, "_systemctl", lambda *args: "ActiveState=active\nUnitFileState=enabled\nMainPID=42"
    )
    monkeypatch.setattr(service_update.shutil, "which", lambda name: "/test/systemctl")
    selected = service_update.inspect_service(
        str(config.config_path), str(config.env_file), "connector"
    )
    assert selected["running"] and selected["enabled"] and selected["pid"] == "42"
    unit.write_text(unit.read_text().replace(str(config.config_path), str(tmp_path / "other.yaml")))
    with pytest.raises(ValueError, match="another installation"):
        service_update.inspect_service(str(config.config_path), str(config.env_file), "connector")


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    unit = tmp_path / "gateway.service"
    unit.write_text("owned definition")
    selected = {
        "unit": str(unit),
        "definition": unit.read_text(),
        "role": "gateway",
        "config": "config.yaml",
        "env_file": ".env",
        "enabled": True,
        "running": True,
        "pid": "42",
        "systemctl": "systemctl",
    }
    current = {"ActiveState": "active", "UnitFileState": "enabled", "MainPID": "42"}
    plan = {
        "python": "python",
        "entry": "acpgw",
        "selection": [None, None, "gateway"],
        "reservation": str(tmp_path / "reservation.json"),
        "result": str(tmp_path / "result.json"),
        "arguments": ["update"],
        "coordinator_fd": 99,
    }
    calls = []
    failures = set()

    def invoke(command, **kwargs):
        if "-c" in command:
            return json.dumps(selected)
        action = "update" if "update" in command else command[-1]
        if "maintenance" in command:
            action = command[command.index("maintenance") + 1]
        calls.append(action)
        if action in failures:
            raise ValueError("injected " + action)
        if action == "prepare":
            Path(plan["reservation"]).write_text("private token")
        if action == "resume":
            Path(plan["reservation"]).unlink()
        return "{}"

    def control(snapshot, action):
        worker.check_definition(snapshot)
        calls.append(action)
        if action in failures:
            raise ValueError("injected " + action)
        if action in {"enable", "disable"}:
            current["UnitFileState"] = "enabled" if action == "enable" else "disabled"
        elif action == "stop":
            current.update(ActiveState="inactive", MainPID="0")
        elif action == "start":
            current.update(ActiveState="active", MainPID="43")

    monkeypatch.setattr(worker, "invoke", invoke)
    monkeypatch.setattr(worker, "control", control)
    monkeypatch.setattr(worker, "state", lambda snapshot: current.copy())
    return plan, selected, current, calls, failures


@pytest.mark.parametrize(
    "enabled,running", [(True, True), (True, False), (False, True), (False, False)]
)
def test_update_restores_exact_enabled_running_state(cycle, enabled, running):
    plan, selected, current, calls, _ = cycle
    selected.update(enabled=enabled, running=running, pid="42" if running else "0")
    current.update(ActiveState="active" if running else "inactive", MainPID=selected["pid"])
    worker.execute(plan)
    assert current["UnitFileState"] == ("enabled" if enabled else "disabled")
    assert (current["ActiveState"] == "active") is running
    assert ("prepare" in calls) is running
    assert ("start" in calls) is running
    assert calls.index("stop") < calls.index("update")


def test_package_failure_restores_service_after_inner_rollback(cycle):
    plan, _, current, calls, failures = cycle
    failures.add("update")
    with pytest.raises(ValueError, match="injected update"):
        worker.execute(plan)
    assert current["ActiveState"] == "active" and current["UnitFileState"] == "enabled"
    assert calls.index("update") < calls.index("start")


def test_stop_failure_resumes_same_process_without_duplicate_start(cycle):
    plan, _, current, calls, failures = cycle
    failures.add("stop")
    with pytest.raises(ValueError, match="injected stop"):
        worker.execute(plan)
    assert "update" not in calls and "start" not in calls
    assert "resume" in calls and current["UnitFileState"] == "enabled"


def test_busy_reservation_keeps_service_and_package_untouched(cycle):
    plan, _, current, calls, failures = cycle
    failures.add("prepare")
    with pytest.raises(ValueError, match="injected prepare"):
        worker.execute(plan)
    assert calls == ["prepare"]
    assert current["ActiveState"] == "active"


@pytest.mark.parametrize("ready_after", [23, None])
def test_cold_start_budget_accepts_slow_health_but_still_times_out(cycle, monkeypatch, ready_after):
    plan, _, current, calls, _ = cycle
    clock = [0.0]
    original = worker.cli
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def delayed(plan, service, *arguments, **kwargs):
        if arguments == ("status",) and (ready_after is None or clock[0] < ready_after):
            raise ValueError("API not ready")
        return original(plan, service, *arguments, **kwargs)

    monkeypatch.setattr(worker, "cli", delayed)
    if ready_after is None:
        with pytest.raises(ValueError, match="did not become healthy"):
            worker.execute(plan)
        assert clock[0] >= 60
        assert worker.failure_details(plan)["failed_phase"] == "restore-services"
    else:
        worker.execute(plan)
        assert 23 <= clock[0] < 60
    assert calls.count("start") == 1 and calls.count("update") == 1
    assert current["ActiveState"] == "active" and current["UnitFileState"] == "enabled"
    assert plan["_package_status"] == "succeeded"


def test_failure_diagnostics_distinguish_busy_from_package_failure(cycle):
    plan, _, _, _, failures = cycle
    failures.add("prepare")
    with pytest.raises(ValueError):
        worker.execute(plan)
    details = worker.failure_details(plan)
    assert details["failed_phase"] == "reserve"
    assert details["package_status"] == "not_started"
    assert "uncertain" in details["reason"]


def test_remote_operation_id_requires_exact_managed_update():
    from uuid import uuid4

    with pytest.raises(ValueError, match="exact-version"):
        installation.maintenance(
            _build_parser().parse_args(["update", "--operation-id", uuid4().hex])
        )


def test_service_result_preserves_remote_operation_identity(tmp_path):
    from uuid import uuid4

    key = uuid4().hex
    path = tmp_path / "result.json"
    worker.record({"result": str(path), "operation_id": key}, "succeeded", "complete", 0)
    assert json.loads(path.read_text())["operation_id"] == key
