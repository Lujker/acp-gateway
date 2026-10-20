"""First-start updater provisioning never changes unrelated installation state."""

from types import SimpleNamespace

from acp_gateway import update_bootstrap as bootstrap


def config(tmp_path, *, enabled=True):
    return SimpleNamespace(
        config_path=tmp_path / "config.yaml",
        env_file=tmp_path / "private.env",
        settings=SimpleNamespace(
            updates=SimpleNamespace(enabled=enabled, spool_dir=None),
            resolved_data_dir=lambda: tmp_path / "data",
        ),
    )


def test_explicit_opt_out_does_not_discover_or_install(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("opt-out must have no side effects")

    monkeypatch.setattr(bootstrap, "installation_info", unexpected)
    result = bootstrap.ensure_updates(config(tmp_path, enabled=False), role="dispatcher")
    assert not result.ready and result.reason == "disabled"
    assert not (tmp_path / "data").exists()


def test_first_start_provisions_only_selected_role_and_verifies_worker(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    calls = []
    monkeypatch.setattr(
        bootstrap, "installation_info", lambda: {"manager": "uv", "source": "registry"}
    )

    monkeypatch.setattr(bootstrap, "inspect_service", lambda *args: {})

    def initialize(path, selected, env, *, role):
        calls.append(("policy", selected, env, role))
        path.parent.mkdir(parents=True)
        path.write_text("{}")

    monkeypatch.setattr(bootstrap, "init_policy", initialize, raising=False)
    monkeypatch.setattr(
        bootstrap, "ensure_companion", lambda path: calls.append(("ensure", path)), raising=False
    )
    monkeypatch.setattr(bootstrap, "validate_policy", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(bootstrap, "worker_ready", lambda *args, **kwargs: True, raising=False)
    result = bootstrap.ensure_updates(cfg, role="dispatcher")
    assert result.ready and result.reason == "ready"
    assert result.policy == tmp_path / "data/update-companions/dispatcher-dispatcher/updater.json"
    assert calls == [
        ("policy", cfg.config_path, cfg.env_file, "dispatcher"),
        ("ensure", result.policy),
    ]
    assert not (tmp_path / "data/update-companions/connector").exists()


def test_matching_runtime_is_not_reinstalled_while_worker_is_running(tmp_path, monkeypatch):
    from acp_gateway import updater_service
    from acp_gateway.update_runner import copy_runtime

    path = tmp_path / "updater.json"
    runtime = updater_service.runtime_path(path)
    runtime.mkdir()
    copy_runtime(runtime)
    calls = []
    monkeypatch.setattr(bootstrap, "manage", lambda action, path: calls.append(action))
    bootstrap.ensure_companion(path)
    assert calls == ["enable"]


def test_docker_host_generates_private_policy_outside_shared_mounts(tmp_path, monkeypatch):
    import json

    cfg = config(tmp_path)
    cfg.settings.resolved_data_dir = lambda: __import__("pathlib").Path("/data")
    (tmp_path / "compose.yaml").write_text("services: {}")
    calls = []
    monkeypatch.setattr(bootstrap, "load_config", lambda *args: cfg, raising=False)
    monkeypatch.setattr(bootstrap, "docker_executable", lambda: "/usr/bin/docker", raising=False)
    monkeypatch.setattr(bootstrap, "ensure_companion", lambda path: calls.append(path))
    monkeypatch.setattr(bootstrap, "policy_worker_ready", lambda value: True, raising=False)
    result = bootstrap.ensure_docker_host(tmp_path)
    assert result.ready
    value = json.loads(result.policy.read_text())
    assert value["auto_refresh"] is True
    assert value["delivery"] == "official" and value["images"] == {}
    assert value["spool_dir"] == str(tmp_path / "state/update-spool")
    assert value["compose_files"] == [str(tmp_path / "compose.yaml")]
    assert value["config"] == "/config/gateway.yaml"
    assert value["env_file"] == "/config/gateway.env"
    assert not result.policy.is_relative_to(tmp_path / "state")
    assert calls == [result.policy]


def test_helper_refresh_stages_reviewed_package_without_overwriting_live_runtime(tmp_path):
    from acp_gateway import updater_service
    from acp_gateway.maintenance_worker import lease
    from acp_gateway.update_runner import copy_runtime

    policy = tmp_path / "updater.json"
    runtime = updater_service.runtime_path(policy)
    runtime.mkdir()
    copy_runtime(runtime)
    (runtime / "worker.py").write_text("old helper")
    with lease(tmp_path / "worker.lock", exclusive=True):
        staged = bootstrap.stage_runtime(policy)
    assert (runtime / "worker.py").read_text() == "old helper"
    assert (staged / "worker.py").read_text() != "old helper"
    assert (staged / "manifest.json").is_file()


def test_runtime_stage_module_cli_runs_in_a_real_subprocess(tmp_path):
    import subprocess
    import sys

    from acp_gateway.updater_service import runtime_path

    policy = tmp_path / "updater.json"
    result = subprocess.run(
        [sys.executable, "-m", "acp_gateway.update_bootstrap", "stage-runtime", str(policy)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (
        runtime_path(policy).with_name(runtime_path(policy).name + ".next") / "manifest.json"
    ).is_file()


def test_unmanaged_native_lifecycle_cannot_be_reported_ready(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(
        bootstrap, "installation_info", lambda: {"manager": "uv", "source": "registry"}
    )

    def inspect(*args):
        raise ValueError("Selected service is not installed")

    monkeypatch.setattr(bootstrap, "inspect_service", inspect, raising=False)
    monkeypatch.setattr(
        bootstrap,
        "init_policy",
        lambda *args, **kwargs: __import__("pytest").fail(
            "must not provision an unsupported lifecycle"
        ),
    )
    result = bootstrap.ensure_updates(cfg, role="dispatcher")
    assert not result.ready and "Selected service is not installed" in result.reason
    assert not (tmp_path / "data").exists()


def test_real_policy_runtime_and_unit_bootstrap_is_idempotent(tmp_path, monkeypatch):
    import json
    import subprocess
    import time
    from pathlib import Path

    import yaml

    from acp_gateway import installation, updater_service
    from acp_gateway.config import load_config
    from acp_gateway.update_journal import Journal
    from acp_gateway.update_runner import runtime_current

    selected = tmp_path / "gateway.yaml"
    env = tmp_path / "gateway.env"
    selected.write_text(
        yaml.safe_dump(
            {"data_dir": str(tmp_path / "data"), "updates": {"enabled": True}, "agents": []}
        )
    )
    env.write_text("UNRELATED=preserved\n")
    original = (selected.read_bytes(), env.read_bytes())
    cfg = load_config(selected, env)

    def info():
        return {"manager": "uv", "source": "registry"}

    monkeypatch.setattr(bootstrap, "installation_info", info)
    monkeypatch.setattr(installation, "installation_info", info)
    monkeypatch.setattr(installation, "_uv", lambda: "/fake/uv")
    monkeypatch.setattr(bootstrap, "inspect_service", lambda *args: {})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    def run(command, **kwargs):
        assert command == ["/fake/uv", "tool", "dir", "--bin"]
        return subprocess.CompletedProcess(command, 0, str(tmp_path / "bin"))

    monkeypatch.setattr(subprocess, "run", run)
    calls = []
    path = tmp_path / "data/update-companions/dispatcher-dispatcher/updater.json"

    def systemctl(*args):
        calls.append(args)
        if args[:2] == ("enable", "--now"):
            policy = json.loads(path.read_text())
            journal = Journal(Path(policy["spool_dir"]) / "operations.db")
            journal.set_worker(
                {
                    "role": "dispatcher",
                    "target": "dispatcher",
                    "backend": "uv",
                    "published": time.time(),
                }
            )
            journal.close()
        return ""

    monkeypatch.setattr(updater_service, "_systemctl", systemctl)
    assert bootstrap.ensure_updates(cfg, role="dispatcher", timeout=0).ready
    assert runtime_current(updater_service.runtime_path(path))
    assert updater_service.unit_path(path).is_file()
    previous = list(calls)
    assert bootstrap.ensure_updates(cfg, role="dispatcher", timeout=0).ready
    assert calls[len(previous) :] == [("enable", "--now", updater_service.unit_path(path).name)]
    assert (selected.read_bytes(), env.read_bytes()) == original
