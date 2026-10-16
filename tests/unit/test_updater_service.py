"""Updater lifecycle stays independent and refuses to kill an installer."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from acp_gateway import remote_update_worker as worker
from acp_gateway import updater_recovery as recovery
from acp_gateway import updater_service as service
from acp_gateway.cli.main import main
from acp_gateway.maintenance_worker import lease
from acp_gateway.update_journal import Journal

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="POSIX lifecycle harness; no native Windows task mutation"
)


@pytest.fixture
def policy(tmp_path, monkeypatch):
    value = {
        "version": 1,
        "backend": "uv",
        "role": "dispatcher",
        "target": "dispatcher",
        "spool_dir": str(tmp_path / "spool"),
        "result_dir": str(tmp_path / "results"),
        "entry": str(tmp_path / "bin/acpgw"),
        "config": str(tmp_path / "config.yaml"),
        "env_file": str(tmp_path / "private.env"),
    }
    path = tmp_path / "updater.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return path, value


def test_systemd_install_copies_base_python_runtime_without_starting(policy, monkeypatch):
    path, value = policy
    calls = []
    monkeypatch.setattr(service, "_systemctl", lambda *a, **kw: calls.append(a) or "")
    assert main(["updater", "--policy", str(path), "install"]) == 0
    unit = service.unit_path(path).read_text()
    assert str(Path(sys._base_executable).absolute()) in unit
    assert str(service.runtime_path(path) / "worker.py") in unit
    assert "-m acp_gateway" not in unit and value["entry"] not in unit
    assert "KillMode=process" in unit and "SendSIGKILL=no" in unit
    assert calls == [("show-environment",), ("daemon-reload",)]
    runtime = service.runtime_path(path)
    assert {p.name for p in runtime.iterdir()} == {
        "worker.py",
        "update_lock.py",
        "update_journal.py",
        "windows_acl.py",
    }
    assert service.manage("enable", path) == 0
    assert calls[-1] == ("enable", "--now", service.unit_path(path).name)
    assert service.manage("uninstall", path) == 0
    assert path.is_file() and runtime.is_dir()
    assert not service.unit_path(path).exists()


def test_install_refuses_unmanaged_unit_and_live_runtime(policy, monkeypatch):
    path, value = policy
    calls = []
    monkeypatch.setattr(service, "_systemctl", lambda *a, **kw: calls.append(a) or "")
    unit = service.unit_path(path)
    unit.parent.mkdir(parents=True)
    unit.write_text("unmanaged")
    with pytest.raises(ValueError, match="unmanaged"):
        service.manage("install", path)
    assert unit.read_text() == "unmanaged" and not calls
    unit.unlink()
    with (
        lease(Path(value["spool_dir"]) / "worker.lock", exclusive=True),
        pytest.raises(ValueError, match="in use"),
    ):
        service.manage("install", path)
    assert not (service.runtime_path(path) / "worker.py").exists()


def test_windows_task_uses_fixed_script_and_json_for_quoted_paths(policy, monkeypatch):
    path, _ = policy
    path = path.parent / "private $x; quoted policy.json"
    calls = []
    monkeypatch.setattr(service.shutil, "which", lambda _: "powershell.exe")

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, '{"installed":true}', "")

    monkeypatch.setattr(service.subprocess, "run", run)
    service._windows("install", path)
    argv, kw = calls[0]
    assert str(path) not in " ".join(argv)
    request = json.loads(kw["input"])
    assert request["policy"] == str(path)
    assert request["arguments"] == subprocess.list2cmdline(service.command(path)[1:])
    assert "acp_gateway" not in request["arguments"]
    assert "ACP Gateway Updater-" in service.WINDOWS_SCRIPT
    assert "$task.Stop" not in service.WINDOWS_SCRIPT


def test_cooperative_stop_waits_for_busy_operation_and_ignores_stale_instance(policy, monkeypatch):
    _, value = policy
    journal = Journal(Path(value["spool_dir"]) / "operations.db")
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
    (Path(value["spool_dir"]) / "worker-stop.json").write_text(json.dumps({"instance": "old"}))
    entered, finish = threading.Event(), threading.Event()

    def execute(*_):
        entered.set()
        assert finish.wait(10)
        return "succeeded", ""

    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(worker, "version_info", lambda _: {"manager": "uv", "source": "registry"})
    thread = threading.Thread(target=worker.run, args=(value,))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError, match="no process was force-stopped"):
            service.stop_worker(value, timeout=0.2)
        assert thread.is_alive() and journal.get(row["id"])["status"] == "running"
        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        assert journal.get(row["id"])["status"] == "succeeded"
        assert journal.worker()["published"] == 0
    finally:
        finish.set()
        thread.join(5)
        journal.close()


def unknown(value):
    journal = Journal(Path(value["spool_dir"]) / "operations.db")
    row = journal.create(
        actor="owner",
        role=value["role"],
        target=value["target"],
        version="0.3.0",
        current_version="0.2.0",
        binding="local",
        expires=time.time() + 120,
        status="unknown",
    )
    return journal, row


@pytest.mark.parametrize("outcome,observed", [("succeeded", "0.3.0"), ("failed", "0.2.0")])
def test_resolution_is_audited_and_unblocks_without_install(policy, monkeypatch, outcome, observed):
    path, value = policy
    journal, row = unknown(value)
    calls = []
    monkeypatch.setattr(recovery, "version_info", lambda _: {"version": observed})
    monkeypatch.setattr(recovery, "cli", lambda p, *a, **kw: calls.append(a))
    try:
        assert (
            main(
                [
                    "updater",
                    "--policy",
                    str(path),
                    "resolve",
                    "--operation",
                    row["id"],
                    "--outcome",
                    outcome,
                    "--note",
                    "Inspected recovered service",
                    "--acknowledge-idle",
                ]
            )
            == 0
        )
        assert journal.get(row["id"])["status"] == outcome
        assert journal.resolution(row["id"])["observed_version"] == observed
        assert calls == [("status",)]
        journal.close()
        journal = Journal(Path(value["spool_dir"]) / "operations.db")
        assert journal.resolution(row["id"])["source"] == "local_operator"
        journal.create(
            actor="owner",
            role="dispatcher",
            target="dispatcher",
            version="0.4.0",
            current_version=observed,
            binding="local",
            expires=time.time() + 120,
        )
    finally:
        journal.close()


def test_resolution_refuses_missing_ack_wrong_version_or_live_worker(policy, monkeypatch):
    path, value = policy
    journal, row = unknown(value)
    monkeypatch.setattr(recovery, "version_info", lambda _: {"version": "0.2.0"})
    try:
        with pytest.raises(ValueError, match="acknowledge-idle"):
            recovery.resolve(path, row["id"], "succeeded", "checked")
        with pytest.raises(ValueError, match="Observed version"):
            recovery.resolve(path, row["id"], "succeeded", "checked", acknowledge_idle=True)
        with (
            lease(Path(value["spool_dir"]) / "worker.lock", exclusive=True),
            pytest.raises(ValueError, match="in use"),
        ):
            recovery.resolve(path, row["id"], "succeeded", "checked", acknowledge_idle=True)
        assert journal.get(row["id"])["status"] == "unknown"
        assert journal.resolution(row["id"]) is None
    finally:
        journal.close()


def test_connector_resolution_uses_local_maintenance_not_dispatcher_api(policy, monkeypatch):
    path, value = policy
    value.update(role="connector", target="home")
    path.write_text(json.dumps(value))
    journal, row = unknown(value)
    calls = []
    monkeypatch.setattr(recovery, "version_info", lambda _: {"version": "0.3.0"})

    def cli(_policy, *argv, **kw):
        calls.append(argv)
        return json.dumps({"state": "running", "reserved": False})

    monkeypatch.setattr(recovery, "cli", cli)
    try:
        recovery.resolve(path, row["id"], "succeeded", "checked", acknowledge_idle=True)
        assert calls == [("maintenance", "--role", "connector", "status")]
        assert journal.get(row["id"])["status"] == "succeeded"
    finally:
        journal.close()


def test_resolution_keeps_unknown_when_health_is_unavailable(policy, monkeypatch):
    path, value = policy
    journal, row = unknown(value)
    monkeypatch.setattr(recovery, "version_info", lambda _: {"version": "0.3.0"})

    def unhealthy(*a, **kw):
        raise ValueError("unavailable")

    monkeypatch.setattr(recovery, "cli", unhealthy)
    try:
        with pytest.raises(ValueError, match="unavailable"):
            recovery.resolve(path, row["id"], "succeeded", "checked", acknowledge_idle=True)
        assert journal.get(row["id"])["status"] == "unknown"
        assert journal.resolution(row["id"]) is None
    finally:
        journal.close()


def test_allow_image_pins_local_artifact_and_refuses_retarget(policy, monkeypatch):
    path, value = policy
    value.update(
        backend="docker",
        docker="/usr/bin/docker",
        project_dir=str(path.parent),
        compose_files=[str(path.parent / "compose.yaml")],
        service="gateway",
        override_file=str(path.parent / "override.json"),
        database=str(path.parent / "gateway.db"),
        work_dir=str(path.parent / "backups"),
        container_spool="/data/update-spool",
        images={},
    )
    path.write_text(json.dumps(value))
    calls = []
    pinned = "sha256:" + "a" * 64

    def invoke(argv, **kw):
        calls.append(argv)
        return json.dumps([{"Id": pinned}])

    monkeypatch.setattr(recovery, "invoke", invoke)
    assert (
        main(
            [
                "updater",
                "--policy",
                str(path),
                "allow-image",
                "--version",
                "0.3.0",
                "--image",
                "example/image@sha256:" + "b" * 64,
            ]
        )
        == 0
    )
    assert json.loads(path.read_text())["images"] == {"0.3.0": pinned}
    assert calls[0][:3] == ["/usr/bin/docker", "image", "inspect"]
    assert recovery.allow_image(path, "0.3.0", pinned) == 0
    old = path.read_bytes()
    with pytest.raises(ValueError, match="immutable"):
        recovery.allow_image(path, "0.4.0", "example/image:latest")
    monkeypatch.setattr(
        recovery, "invoke", lambda *a, **kw: json.dumps([{"Id": "sha256:" + "c" * 64}])
    )
    with pytest.raises(ValueError, match="different image"):
        recovery.allow_image(path, "0.3.0", "sha256:" + "c" * 64)
    assert path.read_bytes() == old
