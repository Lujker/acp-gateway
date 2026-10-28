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
    monkeypatch.setattr(worker, "connector_preflight", lambda _: "")
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


@pytest.mark.parametrize(
    "patch,expected",
    [
        ({"state": "stopped"}, "connector_stopped"),
        ({"state": "unknown"}, "activity_unknown"),
        ({"running_sessions": 2}, "busy"),
        ({"uncertain_sessions": 1}, "activity_uncertain"),
        ({"reserved": True}, "maintenance_reserved"),
        ({"unsupported_agents": 1}, "agent_unsupported"),
        ({}, ""),
    ],
)
def test_connector_preflight_returns_only_safe_codes(native, monkeypatch, patch, expected):
    status = {
        "state": "running",
        "reserved": False,
        "pending_requests": 0,
        "pending_approvals": 0,
        "running_sessions": 0,
        "uncertain_sessions": 0,
        "opening": 0,
        "unsupported_agents": 0,
    }
    status.update(patch)
    monkeypatch.setattr(worker, "cli", lambda *args, **kwargs: json.dumps(status))
    assert worker.connector_preflight(native) == expected


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "{}",
        '{"state":"running","reserved":false,"running_sessions":true}',
        '{"state":"running","reserved":false,"running_sessions":-1}',
    ],
)
def test_connector_preflight_rejects_malformed_activity(native, monkeypatch, payload):
    monkeypatch.setattr(worker, "cli", lambda *args, **kwargs: payload)
    assert worker.connector_preflight(native) == "activity_unknown"


def test_execute_refuses_failed_preflight_without_handoff(native, row, monkeypatch):
    native["role"] = row["role"] = "connector"
    monkeypatch.setattr(
        worker,
        "version_info",
        lambda _: {"version": "0.2.0", "manager": "uv", "source": "registry"},
    )
    monkeypatch.setattr(worker, "connector_preflight", lambda _: "activity_uncertain")
    monkeypatch.setattr(worker, "uv_update", lambda *_: pytest.fail("unexpected handoff"))
    assert worker.execute(native, row) == ("failed", "activity_uncertain")


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"reserved": True},
        {"unsupported_agents": 1},
        {"running_sessions": 1},
        {"uncertain_sessions": 1},
        {"running_sessions": 1, "uncertain_sessions": 1},
    ],
)
def test_worker_and_maintenance_activity_classification_agree(native, monkeypatch, change):
    from acp_gateway.connectors.maintenance import activity_reason

    status = {
        "state": "running",
        "reserved": False,
        "pending_requests": 0,
        "pending_approvals": 0,
        "running_sessions": 0,
        "uncertain_sessions": 0,
        "opening": 0,
        "unsupported_agents": 0,
    }
    status.update(change)
    monkeypatch.setattr(worker, "cli", lambda *args, **kwargs: json.dumps(status))
    expected = {
        "idle": "",
        "reserved": "maintenance_reserved",
        "unsupported": "agent_unsupported",
        "busy": "busy",
        "uncertain": "activity_uncertain",
    }
    assert worker.connector_preflight(native) == expected[activity_reason(status)]


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


def test_official_delivery_pulls_only_pinned_published_image(docker, row, monkeypatch):
    docker.update(images={}, delivery="official")
    image = "ghcr.io/lujker/acp-gateway@sha256:" + "a" * 64
    commands = []
    monkeypatch.setattr(worker, "official_image", lambda version: image, raising=False)

    def invoke(command, **kwargs):
        commands.append(command)
        if "ps" in command:
            return "b" * 64
        if "--format" in command:
            return "sha256:" + "c" * 64
        return ""

    monkeypatch.setattr(worker, "invoke", invoke)
    monkeypatch.setattr(worker, "docker_start", lambda policy: None)
    monkeypatch.setattr(worker, "docker_health", lambda policy, version: None)
    assert worker.docker_update(docker, row) == ("succeeded", "")
    assert commands[0] == [docker["docker"], "image", "pull", image]
    assert (
        json.loads(Path(docker["override_file"]).read_text())["services"]["gateway"]["image"]
        == image
    )
    assert not any(":0.3.0" in arg for command in commands for arg in command)


def test_staged_refresh_checks_all_hashes_before_replacing_any_live_helper(tmp_path):
    from acp_gateway import update_bootstrap
    from acp_gateway.updater_service import runtime_path

    path = tmp_path / "updater.json"
    runtime = runtime_path(path)
    runtime.mkdir()
    (runtime / "worker.py").write_text("old")
    staged = update_bootstrap.stage_runtime(path)
    (staged / "update_journal.py").write_text("tampered")
    with pytest.raises(ValueError, match="digest"):
        worker.install_staged_runtime(runtime, tmp_path / "spool")
    assert (runtime / "worker.py").read_text() == "old"
    update_bootstrap.stage_runtime(path)
    assert worker.install_staged_runtime(runtime, tmp_path / "spool")
    assert (runtime / "worker.py").read_text() != "old"
    assert not staged.exists()


@pytest.mark.parametrize("backend", ["uv", "docker"])
def test_runtime_refresh_uses_only_new_exact_official_package(
    native, row, tmp_path, monkeypatch, backend
):
    native.update(backend=backend, auto_refresh=True, refresh_tool="/usr/bin/uv")
    commands = []
    monkeypatch.setattr(worker, "invoke", lambda command, **kwargs: commands.append(command) or "")
    path = tmp_path / "updater.json"
    assert worker.refresh_after_update(native, row, path)
    if backend == "uv":
        assert commands == [
            [
                native["entry"],
                "--config",
                native["config"],
                "--env-file",
                native["env_file"],
                "updater",
                "--policy",
                str(path),
                "stage-runtime",
            ]
        ]
    else:
        assert commands == [
            [
                "/usr/bin/uv",
                "tool",
                "run",
                "--python",
                "3.12",
                "--from",
                "acp-gateway==0.3.0",
                "acpgw",
                "updater",
                "--policy",
                str(path),
                "stage-runtime",
            ]
        ]


def test_success_is_saved_before_runtime_refresh_and_worker_releases_lease(
    native, row, tmp_path, monkeypatch
):
    from acp_gateway.maintenance_worker import lease

    native["auto_refresh"] = True
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
        status="queued",
    )
    seen = []

    def refresh(policy, operation, path):
        seen.append(journal.get(row["id"])["status"])
        return True

    monkeypatch.setattr(worker, "refresh_after_update", refresh)
    monkeypatch.setattr(worker, "execute", lambda *args: ("succeeded", ""))
    monkeypatch.setattr(
        worker, "version_info", lambda *args: {"manager": "uv", "source": "registry"}
    )
    assert worker.run(native, policy_path=tmp_path / "updater.json") is True
    assert seen == ["succeeded"]
    assert journal.worker()["published"] == 0
    with lease(Path(native["spool_dir"]) / "worker.lock", exclusive=True):
        assert not journal.unfinished()
    journal.close()


def test_image_pin_preserves_host_env_and_survives_ordinary_compose_recreate(docker, tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"ACPGW_UID=1000\nPRIVATE=do-not-change\n")
    docker["image_env"] = str(env)
    image = "ghcr.io/lujker/acp-gateway@sha256:" + "a" * 64
    worker.image_override(docker, image)
    assert (
        env.read_bytes()
        == b"ACPGW_UID=1000\nPRIVATE=do-not-change\nACPGW_IMAGE=" + image.encode() + b"\n"
    )
    compose = Path(__file__).resolve().parents[2] / "deploy/docker/compose.yaml"
    assert "${ACPGW_IMAGE:-acpgw:local}" in compose.read_text()


@pytest.mark.parametrize(
    "change",
    [
        {"delivery": "https://untrusted.invalid"},
        {"auto_refresh": "yes"},
        {"auto_refresh": True, "refresh_tool": "relative/uv"},
        {"image_env": "relative/.env"},
    ],
)
def test_delivery_policy_cannot_smuggle_a_source_or_relative_host_path(docker, tmp_path, change):
    docker.update(change)
    path = tmp_path / "updater.json"
    path.write_text(json.dumps(docker))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        worker.load_policy(path)


def test_slow_artifact_delivery_cannot_mutate_after_confirmation_deadline(docker, row, monkeypatch):
    docker.update(images={}, delivery="official")
    image = "ghcr.io/lujker/acp-gateway@sha256:" + "a" * 64
    commands = []
    monkeypatch.setattr(worker, "official_image", lambda version: image)

    def invoke(command, **kwargs):
        commands.append(command)
        if "pull" in command:
            row["expires"] = 0
        return ""

    monkeypatch.setattr(worker, "invoke", invoke)
    assert worker.docker_update(docker, row) == ("failed", "expired")
    assert not Path(docker["override_file"]).exists()
    assert not any("prepare" in command or "up" in command for command in commands)


def test_missing_staged_runtime_requests_service_recovery_not_clean_worker_exit(
    native, monkeypatch
):
    monkeypatch.setattr(worker.sys, "argv", ["worker.py", "/private/updater.json"])
    monkeypatch.setattr(worker, "load_policy", lambda path: native)
    monkeypatch.setattr(worker, "run", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "install_staged_runtime", lambda *args: False)
    assert worker.main() == 1


@pytest.mark.parametrize("action", ["publish", "apply"])
def test_staged_helper_publication_and_swap_share_a_private_lease(tmp_path, action):
    from acp_gateway import update_bootstrap
    from acp_gateway.maintenance_worker import lease
    from acp_gateway.updater_service import runtime_path

    path = tmp_path / "updater.json"
    runtime = runtime_path(path)
    runtime.mkdir()
    (runtime / "worker.py").write_text("old")
    staged = update_bootstrap.stage_runtime(path)
    with (
        lease(runtime.with_name(runtime.name + ".stage.lock"), exclusive=True),
        pytest.raises(ValueError, match="in use"),
    ):
        if action == "publish":
            update_bootstrap.stage_runtime(path)
        else:
            worker.install_staged_runtime(runtime, tmp_path / "spool")
    assert (runtime / "worker.py").read_text() == "old"
    assert staged.is_dir()
