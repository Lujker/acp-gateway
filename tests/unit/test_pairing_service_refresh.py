from types import SimpleNamespace

import pytest

from acp_gateway.config import AppConfig, SecretStore, Settings


def test_service_arguments_refresh_preserves_selected_paths_and_restores_on_failure(
    tmp_path, monkeypatch
):
    from acp_gateway import service, service_update
    from acp_gateway.cli.connection_service import apply_connection_settings

    config = tmp_path / "selected.yaml"
    env = tmp_path / "custom.env"
    config.write_bytes(b"old-config")
    env.write_bytes(b"old-env")
    unit = tmp_path / "connector.service"
    unit.write_text("old unit")
    cfg = AppConfig(Settings(data_dir=tmp_path), SecretStore({}), config, env)
    selected = {
        "backend": "systemd",
        "role": "connector",
        "unit": str(unit),
        "definition": "old unit",
        "running": True,
        "enabled": True,
        "data_dir": str(tmp_path),
    }
    monkeypatch.setattr(service_update, "inspect_service", lambda *a, **k: selected)
    monkeypatch.setattr(service_update, "preflight_activity", lambda *a, **k: None)
    monkeypatch.setattr(service, "unit_path", lambda *a: unit)
    actions = []
    monkeypatch.setattr(service, "control", lambda action, **kw: actions.append(action))
    monkeypatch.setattr(service, "_systemctl", lambda *a, **kw: actions.append(a[0]) or "")
    from acp_gateway.cli import connector_maintenance

    class IPC:
        def __init__(self, directory):
            pass

        def request(self, *a, **k):
            from httpx import Response

            return Response(200, json={"token": "a" * 32})

    monkeypatch.setattr(connector_maintenance, "LocalClient", IPC)

    def fail(cfg, *, launch):
        unit.write_text("new partial unit")
        raise ValueError("install failed")

    monkeypatch.setattr(service, "install", fail)
    prompts = SimpleNamespace(yes=lambda *a, **k: True)
    with pytest.raises(ValueError, match="install failed"):
        apply_connection_settings(
            cfg,
            object(),
            {config: b"new-config", env: b"new-env"},
            {config: b"old-config", env: b"old-env"},
            tmp_path,
            prompts,
        )
    assert config.read_bytes() == b"old-config"
    assert env.read_bytes() == b"old-env"
    assert unit.read_text() == "old unit"
    assert actions[0] == "stop" and actions[-1] == "start"


def test_connection_section_calls_coordinator(tmp_path, monkeypatch, capsys):
    from acp_gateway.cli import connection_service
    from acp_gateway.cli import wizard as w
    from fakes.setup_prompts import save_answers

    config = tmp_path / "config.yaml"
    config.write_text("agents: []\n")
    (tmp_path / ".env").write_text("")
    (tmp_path / w.STATE).write_text('{"version":1,"role":"connector"}')
    connection = {
        "dispatcher_url": "wss://example/connect",
        "computer_id": "pc",
        "token_file": str(tmp_path / "key"),
        "tls_fingerprint": None,
    }
    monkeypatch.setattr(w, "_connection", lambda *a, **k: connection)
    monkeypatch.setattr(w, "_managed_running", lambda *a: False)
    calls = []

    def apply(*args):
        calls.append(args)
        return None, True

    monkeypatch.setattr(connection_service, "apply_connection_settings", apply)
    assert w.wizard(tmp_path, section="connection", prompts=save_answers()) == 0
    assert len(calls) == 1
    assert calls[0][0].env_file == tmp_path / ".env"
    assert "reinstall with" not in capsys.readouterr().out


@pytest.mark.parametrize(("running", "enabled"), [(True, False), (False, True)])
def test_windows_refresh_uses_guarded_snapshot_and_preserves_flags(
    tmp_path, monkeypatch, running, enabled
):
    from httpx import Response

    from acp_gateway import service, service_update, windows_service
    from acp_gateway.cli import connection_service, connector_maintenance

    monkeypatch.setattr(connection_service, "sys", SimpleNamespace(platform="win32"))
    config = tmp_path / "selected.yaml"
    env = tmp_path / "selected.env"
    config.write_bytes(b"old")
    env.write_bytes(b"env")
    cfg = AppConfig(Settings(data_dir=tmp_path), SecretStore({}), config, env)
    selected = {
        "backend": "windows",
        "role": "connector",
        "definition": "old-definition",
        "running": running,
        "enabled": enabled,
        "data_dir": str(tmp_path),
        "control_id": "old-control",
        "arguments_prefix": "selected-prefix",
    }
    monkeypatch.setattr(service_update, "inspect_service", lambda *a, **kw: dict(selected))
    monkeypatch.setattr(service_update, "preflight_activity", lambda *a: None)
    calls = []

    def request(action, **kwargs):
        calls.append((action, kwargs))
        return {"installed": True}

    monkeypatch.setattr(windows_service, "_request", request)

    class Task:
        control_id = "new-control"

        def __init__(self, config):
            calls.append(("snapshot", {"config": config.config_path, "env": config.env_file}))

        def restore(self):
            pytest.fail("rollback not expected")

    monkeypatch.setattr(windows_service, "SetupTask", Task)

    def install(config, *, launch, windows_setup):
        assert isinstance(windows_setup, Task)
        calls.append(("install", {}))

    monkeypatch.setattr(service, "install", install)

    class IPC:
        def __init__(self, directory):
            pass

        def request(self, *a, **kw):
            return Response(200, json={"token": "a" * 32})

    monkeypatch.setattr(connector_maintenance, "LocalClient", IPC)
    _backup, managed = connection_service.apply_connection_settings(
        cfg,
        object(),
        {config: b"new"},
        {config: b"old"},
        tmp_path,
        SimpleNamespace(yes=lambda *a: True),
    )
    assert managed and config.read_bytes() == b"new"
    disable = next(kwargs for action, kwargs in calls if action == "disable")
    assert disable["expectedControlId"] == "old-control"
    assert disable["expectedDefinition"] == "old-definition"
    assert calls[-1] == (
        "maintenance-flags",
        {"expectedControlId": "new-control", "enabled": enabled},
    )
    assert any(action == "start" for action, _ in calls) == running
