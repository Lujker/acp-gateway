"""Managed updates resolve only owned service paths and preserve explicit intent."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from acp_gateway import service_update, windows_service
from acp_gateway.cli.main import _build_parser
from acp_gateway.installation import maintenance


@pytest.mark.parametrize("explicit", ["config", "env", "environment"])
def test_explicit_selection_never_reads_or_overwrites_the_task(monkeypatch, explicit):
    monkeypatch.setattr(service_update, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        windows_service,
        "installed_selection",
        lambda: pytest.fail("Explicit paths were overridden"),
    )
    monkeypatch.delenv("ACPGW_CONFIG", raising=False)
    monkeypatch.delenv("ACPGW_ENV_FILE", raising=False)
    config, env = None, None
    if explicit == "config":
        config = Path("/chosen/config.yaml")
    elif explicit == "env":
        env = Path("/chosen/.env")
    else:
        monkeypatch.setenv("ACPGW_CONFIG", "/chosen/config.yaml")
    assert service_update.resolve_selection(config, env) == (config, env, None)


def test_windows_default_selection_uses_owned_task(monkeypatch):
    monkeypatch.setattr(service_update, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.delenv("ACPGW_CONFIG", raising=False)
    monkeypatch.delenv("ACPGW_ENV_FILE", raising=False)
    paths = Path("/private/config.yaml"), Path("/private/.env")
    monkeypatch.setattr(windows_service, "installed_selection", lambda: paths)
    assert service_update.resolve_selection(None, None) == (*paths, "connector")


@pytest.mark.parametrize(
    "arguments",
    [
        ["-m", "other", "--config", "/private/c", "--env-file", "/private/e", "connector"],
        ["-m", "acp_gateway", "--config", "relative", "--env-file", "/private/e", "connector"],
        ["-m", "acp_gateway", "--config", "/private/c", "--env-file", "/private/e", "serve"],
    ],
)
def test_unsupported_task_launch_is_refused(monkeypatch, arguments):
    monkeypatch.setattr(
        windows_service, "_request", lambda action: {"installed": True, "arguments": "x"}
    )
    monkeypatch.setattr(windows_service, "_split_arguments", lambda command: arguments)
    with pytest.raises(ValueError, match="supported config/env"):
        windows_service.installed_selection()


def test_task_selection_propagates_ownership_refusal(monkeypatch):
    def unmanaged(action):
        raise ValueError("task owned by another installation")

    monkeypatch.setattr(windows_service, "_request", unmanaged)
    with pytest.raises(ValueError, match="another installation"):
        windows_service.installed_selection()


@pytest.mark.parametrize(
    "arguments",
    [
        ["update", "--restart-uncertain"],
        ["update", "--status", "--with-services", "--restart-uncertain"],
        ["update", "--with-services", "--service-role", "gateway", "--restart-uncertain"],
    ],
)
def test_uncertain_restart_requires_a_connector_cycle(arguments):
    with pytest.raises(ValueError, match=r"restart-uncertain|with-services"):
        maintenance(_build_parser().parse_args(arguments))


@pytest.mark.parametrize("acknowledged,active", [(False, False), (True, False), (True, True)])
def test_preflight_explains_uncertainty_without_bypassing_live_work(
    monkeypatch, acknowledged, active
):
    from acp_gateway.connectors import maintenance as connector

    state = {
        "state": "running",
        "reserved": False,
        "unsupported_agents": 0,
        "pending_requests": int(active),
        "pending_approvals": 0,
        "running_sessions": 0,
        "uncertain_sessions": 1,
        "opening": 0,
    }
    monkeypatch.setattr(connector, "read_status", lambda path: state)
    service = {"role": "connector", "running": True, "data_dir": "/private"}
    if acknowledged and not active:
        service_update.preflight_activity(service, restart_uncertain=True)
    else:
        with pytest.raises(ValueError, match=r"uncertain_sessions=1.*restart-uncertain"):
            service_update.preflight_activity(service, restart_uncertain=acknowledged)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="native Windows argv parser")
@pytest.mark.parametrize(
    "paths",
    [
        [r"C:\private folder\computer.yaml", r"C:\private folder\computer.env"],
        [r"C:\private\кириллица.yaml", r"C:\private\computer.env"],
    ],
)
def test_windows_task_arguments_roundtrip_without_shell(paths):
    import subprocess

    arguments = [
        "-m",
        "acp_gateway",
        "--config",
        paths[0],
        "--env-file",
        paths[1],
        "connector",
        "--token-file",
        r"C:\private\computer.key",
    ]
    assert windows_service._split_arguments(subprocess.list2cmdline(arguments)) == arguments


def test_preflight_refusal_records_current_attempt_without_starting_helper(tmp_path, monkeypatch):
    import json

    from acp_gateway import installation

    monkeypatch.setattr(installation.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(installation, "lease_path", lambda: tmp_path / "installation.lock")
    monkeypatch.setattr(service_update, "resolve_selection", lambda *args: args)
    monkeypatch.setattr(service_update, "inspect_service", lambda *args: {"role": "connector"})

    def refuse(*args, **kwargs):
        raise ValueError("uncertain-only state with sensitive config content")

    monkeypatch.setattr(service_update, "preflight_activity", refuse)
    monkeypatch.setattr(
        installation.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Unexpected helper or uv invocation"),
    )
    args = _build_parser().parse_args(["update", "--with-services"])
    with pytest.raises(ValueError, match="uncertain-only"):
        installation._handoff_service_update(args, 99)
    directory = tmp_path / "updates" / "installation"
    pointer = json.loads((directory / "last-operation.json").read_text())
    result = json.loads(Path(pointer["result"]).read_text())
    assert result["status"] == "failed" and result["failed_phase"] == "preflight"
    assert result["package_status"] == "not_started"
    assert "sensitive" not in json.dumps(result)
