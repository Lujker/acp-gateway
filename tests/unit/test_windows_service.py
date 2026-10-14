"""Windows command boundaries without requiring a Windows service manager."""

import base64
import gzip
import json
import re
import secrets
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_connector_service import computer as computer

from acp_gateway import installation, service
from acp_gateway import windows_service as windows
from acp_gateway.log import register_secret


def test_request_uses_fixed_script_and_json_stdin(monkeypatch):
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout='{"installed":false}', stderr="")

    monkeypatch.setattr(windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(windows.subprocess, "run", run)
    value = 'C:\\folder\\кириллица $(throw "injected") & test'
    assert windows._request("install", directory=value) == {"installed": False}
    assert captured["argv"][:4] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
    ]
    bootstrap = base64.b64decode(captured["argv"][4]).decode("utf-16-le")
    compressed = re.search(r"FromBase64String\('([^']+)'\)", bootstrap).group(1)
    assert gzip.decompress(base64.b64decode(compressed)).decode("utf-8") == windows.SCRIPT
    assert len(subprocess.list2cmdline(captured["argv"])) < 32767
    assert json.loads(captured["input"])["directory"] == value
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "shell" not in captured


@pytest.mark.parametrize("failure", ["timeout", "bad_json", "wrong_shape", "error"])
def test_request_failures_are_actionable_and_redacted(monkeypatch, failure):
    secret = secrets.token_urlsafe(32)
    register_secret(secret)

    def run(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        return SimpleNamespace(
            returncode=1 if failure == "error" else 0,
            stdout="[]" if failure == "wrong_shape" else "not json",
            stderr="ACL rejected: " + secret,
        )

    monkeypatch.setattr(windows.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(windows.subprocess, "run", run)
    with pytest.raises(ValueError) as exc:
        windows._request("status")
    assert secret not in str(exc.value)


@pytest.fixture
def manager(monkeypatch):
    calls = []

    def request(action, **values):
        calls.append((action, values))
        return {"installed": True, "name": "mock-task", "state": 3, "enabled": False}

    monkeypatch.setattr(windows, "_request", request)
    return calls


def test_install_requires_file_logs_before_manager(computer, manager):
    config, launch = computer
    with pytest.raises(ValueError, match=r"logging\.file"):
        windows.install(config, launch)
    assert not manager


@pytest.mark.parametrize("frozen", [False, True])
def test_install_passes_paths_without_secret_contents(computer, manager, monkeypatch, frozen):
    config, launch = computer
    config.settings.logging.file = config.config_path.parent / "connector.log"
    monkeypatch.setattr(windows.sys, "frozen", frozen, raising=False)
    launch = replace(launch, token_file=launch.token_file.parent / 'key & $test "name"')
    assert windows.install(config, launch) == 0
    action, values = manager.pop()
    assert action == "install"
    assert values["arguments"].startswith("--config" if frozen else "-m acp_gateway")
    assert "connector --dispatcher-url" in values["arguments"]
    assert values["privateFiles"] == [
        str(config.config_path),
        str(config.env_file),
        str(launch.token_file),
    ]
    assert config.secrets.get("LOCAL_GOOSE_SECRET").get_secret_value() not in json.dumps(values)
    assert json.loads(values["metadata"])["logFile"] == str(config.settings.logging.file)
    metadata = json.loads(values["metadata"])
    assert metadata["controlDirectory"] == str(config.settings.resolved_data_dir())
    assert "--control-id " + metadata["controlId"] in values["arguments"]


def test_control_requires_connector_and_known_action(manager):
    with pytest.raises(ValueError, match="connector"):
        windows.control("status")
    with pytest.raises(ValueError, match="unknown"):
        windows.control("arbitrary", role="connector")
    assert not manager
    for action in ("enable", "disable", "start", "stop", "restart", "uninstall", "status"):
        assert windows.control(action, role="connector") == 0
    assert [action for action, values in manager] == [
        "enable",
        "disable",
        "start",
        "stop",
        "restart",
        "uninstall",
        "status",
    ]


def test_force_is_explicit_and_only_allowed_for_stop_operations(manager):
    with pytest.raises(ValueError, match="--force"):
        windows.control("enable", role="connector", force=True)
    assert not manager
    windows.control("stop", role="connector", force=True)
    assert manager == [("stop", {"force": True})]


def test_windows_service_dispatch_without_global_platform_patch(computer, manager, monkeypatch):
    # Avoid changing pytest/pathlib's platform while exercising the public service entry points.
    monkeypatch.setattr(service, "sys", SimpleNamespace(platform="win32"))
    config, launch = computer
    config.settings.logging.file = config.config_path.parent / "connector.log"
    assert service.install(config, launch=launch) == 0
    assert service.control("status", role="connector") == 0
    with pytest.raises(ValueError, match="connector"):
        service.install(config)


@pytest.mark.parametrize("enabled,state", [(True, 3), (False, 2), (False, 4)])
def test_maintenance_refuses_enabled_or_active_task(monkeypatch, enabled, state):
    monkeypatch.setattr(installation, "sys", SimpleNamespace(platform="win32"))
    calls = []

    def request(action):
        calls.append(action)
        return {"installed": True, "enabled": enabled, "state": state}

    monkeypatch.setattr(windows, "_request", request)
    with pytest.raises(ValueError, match="disable"):
        installation._service_preflight(uninstall=True)
    assert calls == ["status"]


def test_logs_are_bounded_and_redacted(tmp_path, monkeypatch, capsys):
    path = tmp_path / "connector.log"
    secret = secrets.token_urlsafe(32)
    register_secret(secret)
    path.write_text("old line\n" * 100 + secret + "\n")
    monkeypatch.setattr(windows, "_request", lambda action: {"logFile": str(path)})
    windows.control("logs", role="connector")
    text = capsys.readouterr().out
    assert secret not in text
    assert len(text.splitlines()) == 50


def test_windows_wizard_refuses_enabled_task_before_writing(tmp_path, monkeypatch):
    from acp_gateway.cli import wizard

    monkeypatch.setattr(wizard, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        windows, "_request", lambda action: {"installed": True, "enabled": True, "state": 3}
    )
    with pytest.raises(ValueError, match="Disable the connector task"):
        wizard.wizard(tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_windowless_python_is_selected_on_windows(tmp_path, monkeypatch):
    console = tmp_path / "python.exe"
    windowless = tmp_path / "pythonw.exe"
    console.touch()
    windowless.touch()
    monkeypatch.setattr(
        windows,
        "sys",
        SimpleNamespace(
            platform="win32",
            executable=str(console),
            frozen=False,
        ),
    )
    assert windows._background_executable() == str(windowless)
    windowless.unlink()
    with pytest.raises(ValueError, match="Windowless Python is missing"):
        windows._background_executable()
