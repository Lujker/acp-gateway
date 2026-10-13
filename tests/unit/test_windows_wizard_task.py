"""Wizard save/task rollback boundaries without touching the host Scheduler."""

import json
import secrets
from types import SimpleNamespace

import pytest

from acp_gateway import service, windows_acl, windows_service
from acp_gateway.cli import wizard as w
from fakes.setup_prompts import Answers, save_answers

QUESTION = "Install and start the Windows connector logon task?"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(service, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(w, "_managed_running", lambda *args: False)
    monkeypatch.setattr(windows_acl, "check_private_paths", lambda *args, **kwargs: None)
    key = tmp_path / "computer.key"
    key.write_text("acpc_" + secrets.token_urlsafe(32))
    key.chmod(0o600)
    directory = tmp_path / "config & spaces"
    calls = []

    def request(action, **values):
        calls.append((action, values))
        if action == "snapshot":
            return {"installed": False, "xml": None}
        return {"installed": True, "name": "fake-task"}

    monkeypatch.setattr(windows_service, "_request", request)

    def prompts(*, save=True, task=True):
        values = {
            "Role": "connector",
            "Enrollment key file copied privately from the server": str(key),
            QUESTION: task,
            "Computer ID registered on the server": "work",
        }
        return save_answers(**values) if save else Answers(**values)

    return directory, calls, prompts, request


def test_cancel_only_reads_task_without_saving(setup):
    directory, calls, prompts, _ = setup
    assert w.wizard(directory, prompts=prompts(save=False)) == 0
    assert [action for action, _ in calls] == ["snapshot"]
    assert not directory.exists()


def test_save_installs_and_enables_same_task_generation(setup, capsys):
    directory, calls, prompts, _ = setup
    assert w.wizard(directory, prompts=prompts()) == 0
    assert [action for action, _ in calls] == ["snapshot", "install", "enable"]
    values = calls[1][1]
    assert values["guarded"] is True
    assert values["snapshot"] == {"installed": False, "xml": None}
    control_id = json.loads(values["metadata"])["controlId"]
    assert calls[2][1] == {"expectedControlId": control_id}
    assert (directory / "data").is_dir() and (directory / "logs").is_dir()
    assert "native service setup is unavailable" not in capsys.readouterr().out


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["install", "enable"])
def test_failed_task_operation_restores_configuration(setup, monkeypatch, existing, failure):
    directory, calls, prompts, request = setup
    if existing:
        w.wizard(directory, prompts=prompts(task=False))
    paths = [directory / name for name in ("config.yaml", ".env", w.STATE)]
    originals = {path: path.read_bytes() if path.exists() else None for path in paths}

    def fail(action, **values):
        if action == "snapshot" and existing:
            calls.append((action, values))
            return {"installed": True, "xml": "<old-disabled-task/>"}
        result = request(action, **values)
        if action == failure:
            raise ValueError("injected task failure")
        return result

    monkeypatch.setattr(windows_service, "_request", fail)
    with pytest.raises(ValueError, match="injected task failure"):
        w.wizard(directory, prompts=prompts())
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == originals
    assert calls[-1][0] == "restore"
    assert calls[-1][1]["snapshot"]["installed"] is existing


@pytest.mark.parametrize("failure", ["rollback", "concurrent_edit"])
def test_incomplete_rollback_preserves_settings_for_recovery(setup, monkeypatch, failure):
    directory, _, prompts, request = setup

    def fail(action, **values):
        result = request(action, **values)
        if action == "enable":
            if failure == "concurrent_edit":
                with (directory / "config.yaml").open("a") as stream:
                    stream.write("# concurrent edit\n")
            raise ValueError("start failed")
        if action == "restore" and failure == "rollback":
            raise ValueError("task changed")
        return result

    monkeypatch.setattr(windows_service, "_request", fail)
    with pytest.raises(ValueError, match="rollback failed"):
        w.wizard(directory, prompts=prompts())
    assert all((directory / name).exists() for name in ("config.yaml", ".env", w.STATE))


def test_snapshot_failure_prevents_save(setup, monkeypatch):
    directory, _, prompts, _ = setup

    def reject(*args, **kwargs):
        raise ValueError("task uses another configuration")

    monkeypatch.setattr(windows_service, "_request", reject)
    with pytest.raises(ValueError, match="another configuration"):
        w.wizard(directory, prompts=prompts())
    assert not directory.exists()
