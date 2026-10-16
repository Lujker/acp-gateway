"""Independent updater argv, result correlation, rollback and no replay."""

import json
import sqlite3
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from acp_gateway import remote_update_worker as worker
from acp_gateway.update_journal import Journal


@pytest.fixture
def row():
    return {
        "id": uuid4().hex,
        "role": "dispatcher",
        "target": "dispatcher",
        "version": "0.3.0",
        "current_version": "0.2.0",
        "expires": time.time() + 120,
    }


@pytest.fixture
def native(tmp_path):
    return {
        "version": 1,
        "backend": "uv",
        "role": "dispatcher",
        "target": "dispatcher",
        "spool_dir": str(tmp_path / "spool"),
        "result_dir": str(tmp_path / "results"),
        "entry": str(tmp_path / "bin" / "acpgw"),
        "config": str(tmp_path / "config.yaml"),
        "env_file": str(tmp_path / "private.env"),
    }


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_uv_handoff_pins_version_and_correlates_exact_result(native, row, monkeypatch, status):
    commands = []

    def invoke(command, **kwargs):
        commands.append(command)
        root = Path(native["result_dir"])
        root.mkdir()
        (root / (row["id"] + ".service-result.json")).write_text(
            json.dumps(
                {
                    "operation_id": row["id"],
                    "status": status,
                }
            )
        )

    monkeypatch.setattr(worker, "invoke", invoke)
    monkeypatch.setattr(worker, "version_info", lambda p: {"version": row["version"]})
    result = worker.uv_update(native, row)
    assert result[0] == status
    assert commands == [
        [
            native["entry"],
            "--config",
            native["config"],
            "--env-file",
            native["env_file"],
            "update",
            "--with-services",
            "--service-role",
            "gateway",
            "--version",
            "0.3.0",
            "--operation-id",
            row["id"],
        ]
    ]


def test_unrelated_success_result_cannot_complete_operation(native, row, monkeypatch):
    root = Path(native["result_dir"])
    root.mkdir()
    (root / (row["id"] + ".service-result.json")).write_text(
        json.dumps({"operation_id": uuid4().hex, "status": "succeeded"})
    )
    assert worker.result_for(native, row) is None


@pytest.mark.parametrize("kind", ["timeout", "lost_result"])
def test_uncertain_native_handoff_never_reports_known_failure(native, row, monkeypatch, kind):
    monkeypatch.setattr(
        worker,
        "version_info",
        lambda p: {"version": "0.2.0", "manager": "uv", "source": "registry"},
    )

    def fail(p, r):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(["fixed-entry"], 1)
        raise ValueError("private details")

    monkeypatch.setattr(worker, "uv_update", fail)
    assert worker.execute(native, row) == ("unknown", "unknown")


@pytest.fixture
def docker(native, tmp_path):
    policy = {
        **native,
        "backend": "docker",
        "docker": "/usr/bin/docker",
        "service": "gateway",
        "project_dir": str(tmp_path),
        "compose_files": [str(tmp_path / "compose.yaml")],
        "override_file": str(tmp_path / "image.json"),
        "work_dir": str(tmp_path / "private-backups"),
        "database": str(tmp_path / "gateway.db"),
        "container_spool": "/data/update-spool",
        "images": {"0.3.0": "sha256:" + "a" * 64},
    }
    with sqlite3.connect(policy["database"]) as conn:
        conn.execute("CREATE TABLE evidence(value TEXT)")
        conn.execute("INSERT INTO evidence VALUES ('before')")
    Path(policy["spool_dir"]).mkdir()
    return policy


@pytest.mark.parametrize("healthy", [True, False])
def test_docker_update_preserves_volumes_and_rolls_back_database_on_health_failure(
    docker, row, monkeypatch, healthy
):
    commands, starts = [], []

    def invoke(command, **kwargs):
        commands.append(command)
        if "ps" in command:
            return "b" * 64
        if "inspect" in command and "--format" in command:
            return "sha256:" + "c" * 64
        return ""

    def start(policy):
        starts.append(json.loads(Path(policy["override_file"]).read_text()))
        if len(starts) == 1:
            with sqlite3.connect(policy["database"]) as conn:
                conn.execute("UPDATE evidence SET value='after'")
                conn.execute("PRAGMA user_version=42")

    def health(policy, version):
        if version == "0.3.0" and not healthy:
            raise ValueError("unhealthy")

    monkeypatch.setattr(worker, "invoke", invoke)
    monkeypatch.setattr(worker, "docker_start", start)
    monkeypatch.setattr(worker, "docker_health", health)
    result = worker.docker_update(docker, row)
    assert result == (("succeeded", "") if healthy else ("failed", "rolled_back"))
    assert len(starts) == (1 if healthy else 2)
    assert starts[0]["services"]["gateway"]["image"] == "sha256:" + "a" * 64
    if not healthy:
        assert starts[1]["services"]["gateway"]["image"] == "sha256:" + "c" * 64
    with sqlite3.connect(docker["database"]) as conn:
        assert conn.execute("SELECT value FROM evidence").fetchone()[0] == (
            "after" if healthy else "before"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (42 if healthy else 0)
    assert all("down" not in cmd and "--remove-orphans" not in cmd for cmd in commands)


def test_docker_timeout_does_not_launch_competing_rollback(docker, row, monkeypatch):
    def invoke(command, **kwargs):
        if "ps" in command:
            return "b" * 64
        if "--format" in command:
            return "sha256:" + "c" * 64
        return ""

    monkeypatch.setattr(worker, "invoke", invoke)
    monkeypatch.setattr(
        worker,
        "docker_start",
        lambda p: (_ for _ in ()).throw(subprocess.TimeoutExpired("compose", 1)),
    )
    assert worker.docker_update(docker, row) == ("unknown", "unknown")
    value = json.loads(Path(docker["override_file"]).read_text())
    assert value["services"]["gateway"]["image"] == "sha256:" + "a" * 64


@pytest.mark.parametrize("kind", ["expired", "downgrade", "stale", "unmanaged"])
def test_preflight_denials_never_invoke_installer(native, row, monkeypatch, kind):
    info = {"version": "0.2.0", "manager": "uv", "source": "registry"}
    if kind == "expired":
        row["expires"] = 0
    elif kind == "downgrade":
        row["version"] = "0.1.0"
    elif kind == "stale":
        info["version"] = "0.2.1"
    else:
        info["manager"] = "unmanaged"
    monkeypatch.setattr(worker, "version_info", lambda p: info)
    monkeypatch.setattr(worker, "uv_update", lambda *a: pytest.fail("unexpected installation"))
    assert worker.execute(native, row)[0] == "failed"


def test_companion_restart_does_not_replay_claimed_operation(native, row, monkeypatch):
    journal = Journal(Path(native["spool_dir"]) / "operations.db")
    journal.create(
        key=row["id"],
        actor="owner",
        role=row["role"],
        target=row["target"],
        version=row["version"],
        current_version=row["current_version"],
        binding="local",
        expires=row["expires"],
        status="running",
    )
    journal.close()
    monkeypatch.setattr(
        worker,
        "version_info",
        lambda p: {"version": "0.2.0", "manager": "uv", "source": "registry"},
    )
    monkeypatch.setattr(worker, "execute", lambda *a: pytest.fail("replayed installer"))
    monkeypatch.setattr(worker, "result_for", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        worker.run(native)
    journal = Journal(Path(native["spool_dir"]) / "operations.db")
    assert journal.get(row["id"])["status"] == "unknown"
    journal.close()


@pytest.mark.parametrize(
    "image", ["repo:latest", "repo:0.3.0", "$(private)", "https://user:password@host"]
)
def test_policy_rejects_mutable_or_arbitrary_docker_targets(docker, tmp_path, image):
    docker["images"] = {"0.3.0": image}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(docker))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        worker.load_policy(path)


@pytest.mark.parametrize("value", ["../outside", "-option", "private/../../file", "a" * 33])
def test_mailbox_identity_is_validated_before_host_paths_or_commands(
    docker, row, monkeypatch, value
):
    row["id"] = value
    monkeypatch.setattr(
        worker, "invoke", lambda *a, **kw: pytest.fail("host command from invalid mailbox")
    )
    assert worker.docker_update(docker, row) == ("failed", "conflict")
    with pytest.raises(ValueError):
        worker.result_for(docker, row)
