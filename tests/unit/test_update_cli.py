"""Ordinary update commands: shared artifact gates and local authorization paths."""

import contextlib
import json
import time
from types import SimpleNamespace

import pytest
from packaging.version import Version

from acp_gateway import installation
from acp_gateway.cli import update_command
from acp_gateway.cli.main import _build_parser
from acp_gateway.connectors.protocol import InstallationReport
from acp_gateway.update_journal import Journal


def report(*, manager="uv", source="registry", version="0.2.0"):
    return InstallationReport(
        installed_version=version, manager=manager, source=source, platform="linux"
    )


def parsed(*arguments):
    return _build_parser().parse_args(arguments)


@pytest.fixture
def uv_host(monkeypatch, tmp_path):
    monkeypatch.setattr(update_command, "installation_report", lambda **kw: report())
    monkeypatch.setattr(
        installation,
        "installation_info",
        lambda: {"version": "0.2.0", "manager": "uv", "source": "registry"},
    )
    monkeypatch.setattr(installation.paths, "data_dir", lambda: tmp_path / "data")
    return tmp_path


# --- shared artifact gates -------------------------------------------------


def test_check_reports_published_stable_release(uv_host, monkeypatch, capsys):
    monkeypatch.setattr(installation, "latest_stable_release", lambda: Version("0.3.0"))
    checked = []
    monkeypatch.setattr(
        "acp_gateway.updates.published_uv_version",
        lambda version: checked.append(version) or True,
    )

    assert installation.maintenance(parsed("update", "--check")) == 0

    output = capsys.readouterr().out
    assert checked == ["0.3.0"]
    assert "Installed: 0.2.0" in output
    assert "Latest stable release: 0.3.0" in output
    assert "Update available" in output


def test_check_does_not_offer_an_unpublished_artifact(uv_host, monkeypatch, capsys):
    monkeypatch.setattr(installation, "latest_stable_release", lambda: Version("0.3.0"))
    monkeypatch.setattr("acp_gateway.updates.published_uv_version", lambda version: False)

    assert installation.maintenance(parsed("update", "--check")) == 0

    output = capsys.readouterr().out
    assert "0.3.0" in output
    assert "No supported artifact" in output
    assert "Update available" not in output


def test_check_reports_no_newer_stable_release(uv_host, monkeypatch, capsys):
    monkeypatch.setattr(installation, "latest_stable_release", lambda: Version("0.2.0"))
    monkeypatch.setattr("acp_gateway.updates.published_uv_version", lambda version: True)

    assert installation.maintenance(parsed("update", "--check")) == 0

    assert "No newer stable release" in capsys.readouterr().out


def test_docker_check_is_read_only_and_creates_no_spool(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        update_command, "installation_report", lambda **kw: report(manager="docker")
    )
    config = SimpleNamespace(
        settings=SimpleNamespace(
            updates=SimpleNamespace(enabled=True, spool_dir=None),
            resolved_data_dir=lambda: tmp_path,
        )
    )
    monkeypatch.setattr(update_command, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(installation, "latest_stable_release", lambda: Version("0.3.0"))

    assert installation.maintenance(parsed("update", "--check")) == 0

    assert "local updater is not ready" in capsys.readouterr().out
    assert not (tmp_path / "update-spool").exists()


# --- automatic managed uv selection ----------------------------------------


INFO = {"version": "0.2.0", "manager": "uv", "source": "registry"}


@pytest.fixture
def managed(monkeypatch):
    from acp_gateway import service_update

    monkeypatch.setattr(update_command, "installation_report", lambda **kw: report())
    monkeypatch.setattr(
        service_update, "resolve_selection", lambda config, env, role: (config, env, role)
    )
    monkeypatch.setattr(service_update, "inspect_service", lambda *args: {"role": "connector"})
    monkeypatch.setattr(installation, "latest_stable_release", lambda: Version("0.3.0"))


def test_stable_auto_selects_managed_service(managed, monkeypatch, capsys):
    gated = []
    monkeypatch.setattr(
        update_command,
        "available_exact_release",
        lambda current, version, **kwargs: (
            gated.append((current, version, kwargs)) or (version, "published")
        ),
    )
    args = parsed("update", "--stable")

    assert update_command.run(args, INFO) is None

    assert args.with_services is True
    assert args.service_role == "connector"
    assert args.version == "0.3.0"
    assert args.stable is False
    assert [entry[1] for entry in gated] == ["0.3.0"]
    assert gated[0][0].manager == "uv"
    assert "Warning" in capsys.readouterr().err


def test_exact_version_is_gated_before_the_coordinator(managed, monkeypatch):
    gated = []
    monkeypatch.setattr(
        update_command,
        "available_exact_release",
        lambda current, version, **kwargs: gated.append(version) or (version, "published"),
    )
    args = parsed("update", "--version", "0.3.0")

    assert update_command.run(args, INFO) is None

    assert gated == ["0.3.0"]
    assert args.with_services is True
    assert args.version == "0.3.0"


def test_unpublished_exact_artifact_never_stops_the_service(managed, monkeypatch, capsys):
    monkeypatch.setattr(
        update_command,
        "available_exact_release",
        lambda current, version, **kwargs: (None, "unpublished"),
    )
    args = parsed("update", "--version", "0.3.0")

    with pytest.raises(ValueError, match="artifact"):
        update_command.run(args, INFO)

    assert getattr(args, "with_services", False) is False
    assert "Warning" not in capsys.readouterr().err


def test_stable_without_a_newer_release_does_not_touch_services(managed, monkeypatch, capsys):
    monkeypatch.setattr(installation, "latest_stable_release", lambda: Version("0.2.0"))
    monkeypatch.setattr(
        update_command, "available_exact_release", lambda *a, **k: pytest.fail("gate not needed")
    )
    args = parsed("update", "--stable")

    assert update_command.run(args, INFO) == 0

    assert "No newer stable release" in capsys.readouterr().out
    assert getattr(args, "with_services", False) is False


def test_missing_managed_service_keeps_local_authorization(monkeypatch, capsys):
    from acp_gateway import service_update

    monkeypatch.setattr(update_command, "installation_report", lambda **kw: report())
    monkeypatch.setattr(
        service_update, "resolve_selection", lambda config, env, role: (config, env, role)
    )
    monkeypatch.setattr(
        service_update,
        "inspect_service",
        lambda *args: (_ for _ in ()).throw(ValueError("Selected service is not installed")),
    )
    monkeypatch.setattr(
        update_command, "available_exact_release", lambda *a, **k: pytest.fail("no remote gate")
    )
    args = parsed("update", "--stable")

    assert update_command.run(args, INFO) is None

    assert getattr(args, "with_services", False) is False
    assert "Warning" not in capsys.readouterr().err


def test_foreign_managed_service_is_not_silently_bypassed(monkeypatch):
    from acp_gateway import service_update

    monkeypatch.setattr(update_command, "installation_report", lambda **kw: report())
    monkeypatch.setattr(
        service_update, "resolve_selection", lambda config, env, role: (config, env, role)
    )
    monkeypatch.setattr(
        service_update,
        "inspect_service",
        lambda *args: (_ for _ in ()).throw(ValueError("Service belongs to another installation")),
    )
    args = parsed("update", "--stable")

    with pytest.raises(ValueError, match="another installation"):
        update_command.run(args, INFO)


def test_plain_stable_uses_the_existing_coordinator(managed, monkeypatch, tmp_path, capsys):
    handled = []
    monkeypatch.setattr(
        update_command,
        "available_exact_release",
        lambda current, version, **kwargs: (version, "published"),
    )
    monkeypatch.setattr(
        installation, "_service_update_guard", lambda args: contextlib.nullcontext(None)
    )
    monkeypatch.setattr(
        installation,
        "_handoff_service_update",
        lambda args, descriptor: handled.append((args.version, args.service_role)),
    )
    monkeypatch.setattr(installation, "installation_info", lambda: dict(INFO))
    monkeypatch.setattr(installation, "_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(installation, "_service_preflight", lambda **kwargs: [])
    monkeypatch.setattr(installation.paths, "data_dir", lambda: tmp_path / "data")

    assert installation.maintenance(parsed("update", "--stable")) == 0

    assert handled == [("0.3.0", "connector")]
    assert "Warning" in capsys.readouterr().err


# --- Docker spool submission ------------------------------------------------


def docker_config(tmp_path, *, enabled=True):
    return SimpleNamespace(
        settings=SimpleNamespace(
            updates=SimpleNamespace(enabled=enabled, spool_dir=tmp_path / "spool"),
            resolved_data_dir=lambda: tmp_path,
            gateway=SimpleNamespace(host="127.0.0.1", port=8765),
        )
    )


def seed_worker(config, **overrides):
    journal = Journal(config.settings.updates.spool_dir / "operations.db")
    worker = {
        "published": time.time(),
        "role": "dispatcher",
        "target": "dispatcher",
        "backend": "docker",
        "versions": ["0.3.0"],
    }
    worker.update(overrides)
    journal.set_worker(worker)
    return journal


@pytest.fixture
def docker_env(monkeypatch, tmp_path):
    config = docker_config(tmp_path)
    monkeypatch.setattr(
        update_command, "installation_report", lambda **kw: report(manager="docker")
    )
    monkeypatch.setattr(update_command, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(update_command, "_dispatcher_idle", lambda config: True)
    monkeypatch.setattr(update_command, "_connector_idle", lambda config: True)
    monkeypatch.setattr(installation, "latest_stable_release", lambda: Version("0.3.0"))
    return config


INFO_DOCKER = {"version": "0.2.0", "manager": "unmanaged", "source": "registry"}


def test_docker_stable_submits_a_durable_spool_operation(docker_env, monkeypatch, capsys):
    journal = seed_worker(docker_env)
    monkeypatch.setattr(installation, "_uv", lambda: pytest.fail("gateway must not run uv"))
    monkeypatch.setattr(
        installation.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess")
    )
    monkeypatch.setattr(installation.os, "execv", lambda *a: pytest.fail("no exec"))

    assert update_command.run(parsed("update", "--stable"), INFO_DOCKER) == 0

    rows = journal.recent()
    journal.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "queued"
    assert row["version"] == "0.3.0"
    assert row["current_version"] == "0.2.0"
    assert (row["role"], row["target"]) == ("dispatcher", "dispatcher")
    output = capsys.readouterr()
    assert row["id"] in output.out
    assert "ACPGW_UPDATE_OPERATION=" + row["id"] in output.out
    assert "Warning" in output.err
    assert "attachment" in output.err
    assert not hasattr(update_command, "subprocess")


def test_docker_exact_version_requires_a_prepared_artifact(docker_env, monkeypatch):
    journal = seed_worker(docker_env, versions=[])
    monkeypatch.setattr(update_command, "_dispatcher_idle", lambda config: True)

    with pytest.raises(ValueError, match="artifact"):
        update_command.run(parsed("update", "--version", "0.3.0"), INFO_DOCKER)

    assert not journal.recent()
    journal.close()


def test_docker_busy_refusal_is_persisted_not_queued(docker_env, monkeypatch):
    journal = seed_worker(docker_env)
    monkeypatch.setattr(update_command, "_dispatcher_idle", lambda config: False)

    with pytest.raises(ValueError, match="busy"):
        update_command.run(parsed("update", "--stable"), INFO_DOCKER)

    rows = journal.recent()
    assert [row["status"] for row in rows] == ["failed"]
    assert rows[0]["reason"] == "busy"
    assert not journal.unfinished()
    journal.close()


def test_docker_disabled_updates_refuse_the_spool_path(docker_env, monkeypatch, tmp_path):
    config = docker_config(tmp_path, enabled=False)
    monkeypatch.setattr(update_command, "load_config", lambda *a, **k: config)
    journal = seed_worker(config)

    with pytest.raises(ValueError, match="disabled"):
        update_command.run(parsed("update", "--stable"), INFO_DOCKER)

    assert not journal.recent()
    journal.close()


def test_docker_unknown_operation_blocks_a_new_submission(docker_env, monkeypatch):
    journal = seed_worker(docker_env)
    existing = journal.create(
        actor="cli",
        role="dispatcher",
        target="dispatcher",
        version="0.3.0",
        current_version="0.2.0",
        binding="x",
        expires=time.time() + 300,
        status="unknown",
    )

    with pytest.raises(ValueError, match="busy"):
        update_command.run(parsed("update", "--stable"), INFO_DOCKER)

    assert journal.get(existing["id"])["status"] == "unknown"
    assert [row["status"] for row in journal.recent()] == ["failed", "unknown"]
    journal.close()


def test_docker_dry_run_submits_nothing(docker_env, monkeypatch, capsys):
    journal = seed_worker(docker_env)

    assert update_command.run(parsed("update", "--stable", "--dry-run"), INFO_DOCKER) == 0

    assert not journal.recent()
    journal.close()
    output = capsys.readouterr()
    assert "Would submit 0.3.0" in output.out
    assert "Warning" not in output.err


@pytest.mark.parametrize(
    "status,code", [("queued", 2), ("running", 2), ("unknown", 2), ("failed", 1), ("succeeded", 0)]
)
def test_docker_status_reads_the_durable_spool(docker_env, capsys, status, code):
    journal = seed_worker(docker_env)
    journal.create(
        actor="cli",
        role="dispatcher",
        target="dispatcher",
        version="0.3.0",
        current_version="0.2.0",
        binding="x",
        expires=time.time() + 300,
        status=status,
    )

    assert update_command.run(parsed("update", "--status"), INFO_DOCKER) == code

    assert json.loads(capsys.readouterr().out)["status"] == status
    journal.close()


def test_docker_status_without_a_spool_is_read_only(monkeypatch, tmp_path, capsys):
    config = docker_config(tmp_path)
    monkeypatch.setattr(
        update_command, "installation_report", lambda **kw: report(manager="docker")
    )
    monkeypatch.setattr(update_command, "load_config", lambda *a, **k: config)

    assert update_command.run(parsed("update", "--status"), INFO_DOCKER) == 0

    assert "No recorded maintenance operation" in capsys.readouterr().out
    assert not (tmp_path / "spool").exists()


def test_docker_scope_follows_the_worker_heartbeat(docker_env):
    journal = seed_worker(docker_env, role="connector", target="home")

    assert update_command.run(parsed("update", "--stable"), INFO_DOCKER) == 0

    row = journal.recent()[0]
    assert (row["role"], row["target"]) == ("connector", "home")
    journal.close()


# --- preserved advanced and read-only paths ---------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["update", "--with-services"],
        ["update", "--from", "https://example.com/acp_gateway-0.3.0-py3-none-any.whl"],
        ["update", "--rollback"],
        ["update", "--service-role", "connector"],
        ["update", "--handoff-file", "handoff.json"],
        ["update", "--constraints", "requirements.txt"],
        ["update", "--stable", "--with-services"],
    ],
)
def test_advanced_switches_keep_the_documented_path(arguments, monkeypatch):
    monkeypatch.setattr(update_command, "load_config", lambda *a, **k: pytest.fail("config"))
    monkeypatch.setattr(update_command, "installation_report", lambda **k: pytest.fail("report"))

    assert update_command.run(parsed(*arguments), INFO) is None


def test_uv_dry_run_has_no_warning_or_mutation(managed, monkeypatch, capsys):
    gated = []
    monkeypatch.setattr(
        update_command,
        "available_exact_release",
        lambda current, version, **kwargs: gated.append(version) or (version, "published"),
    )
    args = parsed("update", "--stable", "--dry-run")

    assert update_command.run(args, INFO) is None

    assert gated == ["0.3.0"]
    assert getattr(args, "with_services", False) is True
    assert "Warning" not in capsys.readouterr().err


def test_dispatcher_idle_requires_an_idle_owner_state(monkeypatch, tmp_path):
    import httpx

    from acp_gateway import daemon

    config = docker_config(tmp_path)
    monkeypatch.setattr(
        daemon, "owner_token", lambda config: SimpleNamespace(get_secret_value=lambda: "token")
    )
    payload = {}

    def handler(request):
        assert request.headers["Authorization"] == "Bearer token"
        if isinstance(payload["state"], int):
            return httpx.Response(payload["state"], request=request)
        return httpx.Response(200, json=payload["state"], request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    idle = {"reserved": False, "running_jobs": 0, "model_operations": 0}
    payload["state"] = idle
    assert update_command._dispatcher_idle(config, client=client) is True
    payload["state"] = {**idle, "running_jobs": 1}
    assert update_command._dispatcher_idle(config, client=client) is False
    payload["state"] = {**idle, "unresolved_model_states": 2}
    assert update_command._dispatcher_idle(config, client=client) is False
    payload["state"] = {}
    assert update_command._dispatcher_idle(config, client=client) is False
    payload["state"] = {**idle, "reserved": "no"}
    assert update_command._dispatcher_idle(config, client=client) is False
    payload["state"] = 503
    assert update_command._dispatcher_idle(config, client=client) is False


def test_dispatcher_idle_without_an_owner_token_is_not_idle(monkeypatch, tmp_path):
    from acp_gateway import daemon

    config = docker_config(tmp_path)
    monkeypatch.setattr(
        daemon, "owner_token", lambda config: (_ for _ in ()).throw(ValueError("missing token"))
    )

    assert update_command._dispatcher_idle(config) is False


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"state": "running", "reserved": False, "pending_requests": 0}, True),
        ({"state": "running", "reserved": False, "pending_requests": 1}, False),
        ({"state": "running", "reserved": True, "pending_requests": 0}, False),
        ({"state": "unknown", "reserved": False, "pending_requests": 0}, False),
        ({"state": "stopped", "reserved": False, "pending_requests": 0}, False),
    ],
)
def test_connector_idle_reuses_the_existing_admission_helper(
    monkeypatch, tmp_path, status, expected
):
    from acp_gateway.connectors import maintenance as connector

    config = docker_config(tmp_path)
    monkeypatch.setattr(connector, "read_status", lambda directory: status)

    assert update_command._connector_idle(config) is expected


def test_release_metadata_is_bounded(monkeypatch):
    import httpx

    def oversized(client, url):
        return httpx.Response(
            200,
            content=b" " * 300000,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", oversized)
    with pytest.raises(ValueError, match="limit"):
        installation.latest_stable_release()


def test_check_retrieval_failure_is_a_controlled_error(uv_host, monkeypatch, capsys):
    import httpx

    monkeypatch.setattr(
        installation,
        "latest_stable_release",
        lambda: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )

    from acp_gateway.cli.main import main

    assert main(["update", "--check"]) == 1
    assert "offline" in capsys.readouterr().err


def test_main_wires_docker_exact_update_to_the_spool(docker_env, capsys):
    from acp_gateway.cli.main import main

    journal = seed_worker(docker_env)

    assert main(["update", "--version", "0.3.0"]) == 0

    row = journal.recent()[0]
    assert (row["status"], row["version"]) == ("queued", "0.3.0")
    journal.close()
    assert "ACPGW_UPDATE_OPERATION=" in capsys.readouterr().out


def test_docker_status_remains_readable_when_updates_are_disabled(monkeypatch, tmp_path, capsys):
    config = docker_config(tmp_path, enabled=False)
    monkeypatch.setattr(
        update_command, "installation_report", lambda **kw: report(manager="docker")
    )
    monkeypatch.setattr(update_command, "load_config", lambda *a, **k: config)
    journal = seed_worker(config)
    journal.create(
        actor="cli",
        role="dispatcher",
        target="dispatcher",
        version="0.3.0",
        current_version="0.2.0",
        binding="x",
        expires=time.time() + 300,
        status="running",
    )

    assert update_command.run(parsed("update", "--status"), INFO_DOCKER) == 2

    assert json.loads(capsys.readouterr().out)["status"] == "running"
    journal.close()
