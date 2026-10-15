"""CLI admission tokens stay private and cannot overwrite another reservation."""

import json
import os
from types import SimpleNamespace

import httpx
import pytest

from acp_gateway.cli import maintenance_mode as mode

TOKEN = "ab" * 16


@pytest.fixture
def context(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("prepare"):
            return httpx.Response(200, json={"token": TOKEN})
        return httpx.Response(200, json={"reserved": False})

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://127.0.0.1")
    config = SimpleNamespace(settings=SimpleNamespace(resolved_data_dir=lambda: tmp_path))
    args = SimpleNamespace(maintenance_command="prepare", reservation_file=None, force=False)
    with client:
        yield client, config, args, calls, tmp_path / "maintenance.json"


def test_prepare_status_resume_keeps_token_private(context, capsys):
    client, config, args, calls, path = context
    mode.run(client, config, args)
    assert json.loads(path.read_text())["token"] == TOKEN
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    args.maintenance_command = "status"
    mode.run(client, config, args)
    args.maintenance_command = "resume"
    mode.run(client, config, args)
    assert json.loads(calls[-1].content) == {"token": TOKEN}
    assert not path.exists()
    assert TOKEN not in capsys.readouterr().out


def test_existing_file_prevents_prepare_before_http(context):
    client, config, args, calls, path = context
    path.write_text("existing")
    with pytest.raises(FileExistsError):
        mode.run(client, config, args)
    assert not calls
    assert path.read_text() == "existing"


def test_failed_prepare_removes_only_its_empty_file(context, monkeypatch):
    client, config, args, _, path = context
    monkeypatch.setattr(
        mode, "request", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("busy"))
    )
    with pytest.raises(ValueError, match="busy"):
        mode.run(client, config, args)
    assert not path.exists()


def test_persistence_failure_releases_its_server_reservation(context, monkeypatch):
    client, config, args, calls, path = context

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(mode.os, "fsync", fail)
    with pytest.raises(OSError, match="disk failure"):
        mode.run(client, config, args)
    assert [r.url.path for r in calls] == ["/maintenance/prepare", "/maintenance/resume"]
    assert json.loads(calls[-1].content) == {"token": TOKEN}
    assert not path.exists()


def test_failed_resume_preserves_token_for_retry(context, monkeypatch):
    client, config, args, _, path = context
    mode.run(client, config, args)
    args.maintenance_command = "resume"

    def fail(*args, **kwargs):
        raise ValueError("wrong reservation")

    monkeypatch.setattr(mode, "request", fail)
    with pytest.raises(ValueError, match="wrong reservation"):
        mode.run(client, config, args)
    assert json.loads(path.read_text())["token"] == TOKEN


def test_missing_token_requires_explicit_owner_recovery(context):
    client, config, args, calls, _ = context
    args.maintenance_command = "resume"
    with pytest.raises(ValueError, match="owner recovery"):
        mode.run(client, config, args)
    assert not calls
    args.force = True
    mode.run(client, config, args)
    assert json.loads(calls[-1].content) == {"force": True}


@pytest.mark.parametrize("data", ["bad json", '{"version":true,"token":"invalid"}'])
def test_malformed_token_file_is_not_sent_or_deleted(context, data):
    client, config, args, calls, path = context
    path.write_text(data)
    path.chmod(0o600)
    args.maintenance_command = "resume"
    with pytest.raises(ValueError):
        mode.run(client, config, args)
    assert not calls
    assert path.read_text() == data


def test_uncertain_restart_ack_is_only_forwarded_for_explicit_connector_prepare(context):
    client, config, args, calls, _ = context
    args.role = "connector"
    args.restart_uncertain = True
    mode.run(client, config, args)
    assert json.loads(calls[0].content) == {"restart_uncertain": True}


def test_gateway_cannot_ack_connector_uncertainty(context):
    client, config, args, calls, _ = context
    args.role = "gateway"
    args.restart_uncertain = True
    with pytest.raises(ValueError, match="only for connector"):
        mode.run(client, config, args)
    assert not calls
