"""Durable confirmation, owner binding and exactly-once mailbox admission."""

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from acp_gateway.connectors.protocol import InstallationReport, UpdateOperation, UpdateRequest
from acp_gateway.core import GatewayError
from acp_gateway.update_journal import Journal
from acp_gateway.update_service import LocalUpdates, UpdateService
from acp_gateway.updates import UpdateInventory


def info():
    return InstallationReport(
        installed_version="0.2.0", manager="uv", source="registry", platform="linux"
    )


@pytest.fixture
def operations(tmp_path, monkeypatch):
    config = SimpleNamespace(
        settings=SimpleNamespace(
            updates=SimpleNamespace(enabled=True, spool_dir=tmp_path / "spool"),
            resolved_data_dir=lambda: tmp_path,
        )
    )
    local = LocalUpdates(config, role="dispatcher", target="dispatcher")
    monkeypatch.setattr("acp_gateway.update_service.installation_report", lambda **kw: info())
    spool = Journal(local.path)
    spool.set_worker(
        {"published": time.time(), "role": "dispatcher", "target": "dispatcher", "backend": "uv"}
    )
    inventory = UpdateInventory(package_lookup=lambda version: True, local_report=local.report)
    inventory.latest = "0.3.0"
    journal = Journal(tmp_path / "control.db")
    service = UpdateService(inventory, journal, local)
    yield service, spool, config
    journal.close()
    spool.close()


async def test_confirmation_is_durable_owner_bound_and_single_admission(operations, tmp_path):
    service, spool, _ = operations
    row = await service.prepare("owner", "dispatcher", "dispatcher")
    assert not spool.unfinished()
    reopened = Journal(tmp_path / "control.db")
    assert reopened.get(row["id"])["version"] == "0.3.0"
    reopened.close()
    with pytest.raises(GatewayError):
        await service.confirm("other", row["id"])
    results = await asyncio.gather(*(service.confirm("owner", row["id"]) for _ in range(5)))
    assert all(r["status"] in {"dispatching", "queued"} for r in results)
    assert len(spool.unfinished()) == 1
    assert spool.unfinished()[0]["version"] == "0.3.0"
    await service.status("owner", row["id"])
    assert len(spool.unfinished()) == 1
    spool.transition(row["id"], ("queued",), "succeeded")
    assert (await service.status("owner", row["id"]))["status"] == "succeeded"


@pytest.mark.parametrize("change", ["cancel", "expiry", "disabled", "version"])
async def test_stale_or_cancelled_confirmation_does_not_queue(operations, change):
    service, spool, config = operations
    row = await service.prepare("owner", "dispatcher", "dispatcher")
    if change == "cancel":
        service.cancel("owner", row["id"])
    elif change == "expiry":
        service.journal.conn.execute("UPDATE operations SET expires=0")
    elif change == "disabled":
        config.settings.updates.enabled = False
    else:
        service.local.report = lambda: info().model_copy(
            update={"installed_version": "0.2.1", "can_update": True}
        )
    result = await service.confirm("owner", row["id"])
    assert result["status"] == "cancelled"
    assert not spool.unfinished()


async def test_lost_ack_becomes_unknown_and_status_never_replays(operations):
    service, spool, _ = operations
    calls = []
    original = service._dispatch

    async def lost(row, action):
        calls.append(action)
        result = await original(row, action)
        if action == "execute":
            raise TimeoutError
        return result

    service._dispatch = lost
    row = await service.prepare("owner", "dispatcher", "dispatcher")
    assert (await service.confirm("owner", row["id"]))["status"] == "unknown"
    await service.confirm("owner", row["id"])
    assert calls == ["execute"]
    assert (await service.status("owner", row["id"]))["status"] == "queued"
    assert calls == ["execute", "status"] and len(spool.unfinished()) == 1


@pytest.mark.parametrize("reason", ["disabled", "worker_stopped", "expired"])
def test_connector_admission_denial_is_durable_and_cannot_later_replay(operations, reason):
    service, spool, config = operations
    if reason == "disabled":
        config.settings.updates.enabled = False
    elif reason == "worker_stopped":
        spool.set_worker({"published": 0})
    request = UpdateRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        action="execute",
        actor="owner",
        target_version="0.3.0",
        current_version="0.2.0",
        expires=float(time.time() - 1 if reason == "expired" else time.time() + 60),
    )
    result = service.local.request(request)
    assert result.status == "failed" and result.reason == reason
    config.settings.updates.enabled = True
    assert service.local.request(request) == result
    assert not spool.unfinished()


def test_same_operation_id_with_changed_parameters_is_rejected(operations):
    service, spool, _ = operations
    request = UpdateRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        action="execute",
        actor="owner",
        target_version="0.3.0",
        current_version="0.2.0",
        expires=float(time.time() + 60),
    )
    assert service.local.request(request).status == "queued"
    assert (
        service.local.request(request.model_copy(update={"target_version": "0.4.0"})).reason
        == "conflict"
    )
    assert len(spool.unfinished()) == 1


def test_busy_denial_does_not_become_an_implicit_queue_later(operations):
    service, spool, _ = operations
    request = UpdateRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        action="execute",
        actor="owner",
        target_version="0.3.0",
        current_version="0.2.0",
        expires=float(time.time() + 60),
    )
    service.local.request(request)
    second = request.model_copy(update={"operation": uuid4()})
    assert service.local.request(second).reason == "busy"
    spool.transition(request.operation, ("queued",), "succeeded")
    assert service.local.request(second).reason == "busy"
    assert not spool.unfinished()


async def test_connector_epoch_change_between_prepare_and_confirm_refuses_start(operations):
    service, _, _ = operations
    binding = [str(uuid4())]
    calls = []

    async def target(role, name):
        return info().model_copy(update={"can_update": True}), binding[0]

    async def dispatch(row, action):
        calls.append(action)
        return UpdateOperation(operation=uuid4(), status="queued")

    service._target = target
    service._dispatch = dispatch
    row = await service.prepare("owner", "connector", "home")
    binding[0] = str(uuid4())
    assert (await service.confirm("owner", row["id"]))["reason"] == "stale"
    assert not calls


async def test_docker_version_must_be_prepared_and_local_policy_rechecked(operations, monkeypatch):
    service, spool, _ = operations
    monkeypatch.setattr(
        "acp_gateway.update_service.installation_report",
        lambda **kw: info().model_copy(update={"manager": "docker"}),
    )
    service.inventory.image_lookup = lambda version: "ghcr.io/lujker/acp-gateway@sha256:" + "a" * 64
    pulse = {
        "published": time.time(),
        "role": "dispatcher",
        "target": "dispatcher",
        "backend": "docker",
        "versions": [],
    }
    spool.set_worker(pulse)
    with pytest.raises(GatewayError, match="не подготовлена"):
        await service.prepare("owner", "dispatcher", "dispatcher")
    pulse["versions"] = ["0.3.0"]
    spool.set_worker(pulse)
    row = await service.prepare("owner", "dispatcher", "dispatcher")
    pulse["versions"] = []
    spool.set_worker(pulse)
    result = await service.confirm("owner", row["id"])
    assert result["status"] == "failed" and result["reason"] == "version_not_allowed"
    assert not spool.unfinished()


async def test_unpublished_docker_version_cannot_create_confirmation(operations):
    service, spool, _ = operations
    service.local.report = lambda: info().model_copy(
        update={"manager": "docker", "can_update": True}
    )
    service.local.version_allowed = lambda version: True
    service.inventory.image_lookup = lambda version: None
    with pytest.raises(GatewayError, match="артефакт"):
        await service.prepare("owner", "dispatcher", "dispatcher")
    assert not service.journal.recent(actor="owner")
    assert not spool.unfinished()


def test_official_worker_does_not_need_per_release_allowlist(operations, monkeypatch):
    from acp_gateway import update_service

    service, spool, _ = operations
    spool.set_worker(
        {
            "published": time.time(),
            "role": "dispatcher",
            "target": "dispatcher",
            "backend": "docker",
            "versions": [],
            "delivery": "official",
        }
    )
    monkeypatch.setattr(
        update_service,
        "official_image",
        lambda version: "ghcr.io/lujker/acp-gateway@sha256:" + "a" * 64,
        raising=False,
    )
    assert service.local.version_allowed("0.3.0")


def test_busy_admission_is_persisted_not_a_deferred_queue(operations):
    _service, spool, cfg = operations
    local = LocalUpdates(cfg, role="dispatcher", target="dispatcher", idle_check=lambda: False)
    request = UpdateRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        action="execute",
        actor="owner",
        target_version="0.3.0",
        current_version="0.2.0",
        expires=time.time() + 60,
    )
    assert local.request(request).reason == "busy"
    assert not spool.unfinished()
    local.idle_check = lambda: True
    assert local.request(request).status == "failed"
    assert not spool.unfinished()
