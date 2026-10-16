import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from filelock import FileLock

from acp_gateway.cli.connector_maintenance import LocalClient
from acp_gateway.connectors.lifecycle import control_record
from acp_gateway.connectors.maintenance import (
    FIELDS,
    REQUEST,
    STATUS,
    ConnectorMaintenance,
    read_status,
    watch,
)


@pytest.fixture
def controller():
    return ConnectorMaintenance([SimpleNamespace(alias="hermes", backend="hermes")], {})


async def test_live_local_ipc_reserves_and_resumes_without_owner_api(tmp_path, controller):
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(watch(tmp_path, instance, controller, interval=0.01))
        client = LocalClient(tmp_path, timeout=15)
        try:
            async with asyncio.timeout(10):
                while not (tmp_path / STATUS).exists():
                    await asyncio.sleep(0.01)
            prepared = await asyncio.to_thread(client.request, "POST", "/maintenance/prepare")
            assert prepared.status_code == 200
            token = prepared.json()["token"]
            assert controller.status()["reserved"]
            with pytest.raises(Exception, match="reserved"):
                controller.check_admission()
            wrong = await asyncio.to_thread(
                client.request, "POST", "/maintenance/resume", json={"token": "0" * 32}
            )
            assert wrong.status_code == 409
            resumed = await asyncio.to_thread(
                client.request, "POST", "/maintenance/resume", json={"token": token}
            )
            assert resumed.status_code == 200
            assert not controller.status()["reserved"]
            assert token not in (tmp_path / STATUS).read_text()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not (tmp_path / STATUS).exists()


@pytest.mark.parametrize("field", [*FIELDS, "unsupported"])
def test_prepare_fails_closed_for_each_busy_or_unknown_state(controller, field):
    counts = dict.fromkeys(FIELDS, 0)
    if field == "unsupported":
        controller.profiles[0].backend = "remote"
    else:
        counts[field] = 1
        controller.managers["hermes"] = SimpleNamespace(maintenance_status=lambda: counts)
    with pytest.raises(ValueError, match="refused"):
        controller.apply("prepare")
    assert not controller.status()["reserved"]


@pytest.mark.parametrize("kind", ["stale", "wrong_instance", "invalid"])
def test_activity_reader_does_not_trust_unverifiable_record(tmp_path, controller, kind):
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        record = {
            "version": 1,
            "instance": instance,
            "published": time.time(),
            **controller.status(),
        }
        if kind == "stale":
            record["published"] -= 20
        elif kind == "wrong_instance":
            record["instance"] = "0" * 32
        else:
            record["pending_requests"] = False
        (tmp_path / STATUS).write_text(json.dumps(record))
        assert read_status(tmp_path) == {"state": "unknown", "idle_observed": False}


async def test_previous_process_request_cannot_reserve_replacement(tmp_path, controller):
    (tmp_path / REQUEST).write_text(
        json.dumps(
            {
                "version": 1,
                "instance": "0" * 32,
                "request": "1" * 32,
                "expires": time.time() + 10,
                "operation": "prepare",
            }
        )
    )
    with control_record(tmp_path) as instance:
        task = asyncio.create_task(watch(tmp_path, instance, controller, interval=0.01))
        try:
            await asyncio.sleep(0.08)
            assert not controller.status()["reserved"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("acknowledged", [False, True])
def test_uncertain_restart_needs_explicit_ack_and_preserves_quarantine(controller, acknowledged):
    counts = dict.fromkeys(FIELDS, 0)
    counts["uncertain_sessions"] = 1
    manager = SimpleNamespace(maintenance_status=lambda: counts)
    controller.managers["hermes"] = manager
    if acknowledged:
        result = controller.apply("prepare", restart_uncertain=True)
        assert result["token"] and controller.status()["reserved"]
        assert controller.status()["uncertain_sessions"] == 1
        controller.apply("resume", token=result["token"])
        assert controller.status()["uncertain_sessions"] == 1
    else:
        with pytest.raises(ValueError, match="refused"):
            controller.apply("prepare")
        assert not controller.status()["reserved"]


@pytest.mark.parametrize(
    "field", [f for f in FIELDS if f != "uncertain_sessions"] + ["unsupported"]
)
def test_uncertain_ack_never_permits_active_or_unsupported_work(controller, field):
    counts = dict.fromkeys(FIELDS, 0)
    counts["uncertain_sessions"] = 1
    controller.managers["hermes"] = SimpleNamespace(maintenance_status=lambda: counts)
    if field == "unsupported":
        controller.profiles[0].backend = "remote"
    else:
        counts[field] = 1
    with pytest.raises(ValueError, match="refused"):
        controller.apply("prepare", restart_uncertain=True)
    assert not controller.status()["reserved"]


async def test_uncertain_restart_ack_crosses_live_ipc_and_still_reserves_atomically(
    tmp_path, controller
):
    counts = dict.fromkeys(FIELDS, 0)
    counts["uncertain_sessions"] = 1
    controller.managers["hermes"] = SimpleNamespace(maintenance_status=lambda: counts)
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(watch(tmp_path, instance, controller, interval=0.01))
        client = LocalClient(tmp_path, timeout=10)
        try:
            async with asyncio.timeout(5):
                while not (tmp_path / STATUS).exists():
                    await asyncio.sleep(0.01)
            denied = await asyncio.to_thread(client.request, "POST", "/maintenance/prepare")
            assert denied.status_code == 409
            prepared = await asyncio.to_thread(
                client.request, "POST", "/maintenance/prepare", json={"restart_uncertain": True}
            )
            assert prepared.status_code == 200
            assert controller.status()["reserved"] and counts["uncertain_sessions"] == 1
            with pytest.raises(Exception, match="reserved"):
                controller.check_admission()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_activity_write_failure_is_logged_and_watcher_recovers(
    tmp_path, controller, monkeypatch
):
    from acp_gateway.connectors import maintenance

    original = maintenance._atomic
    attempts = 0
    warnings = []

    def flaky(path, content):
        nonlocal attempts
        if path.name == STATUS:
            attempts += 1
            if attempts <= 2:
                raise PermissionError("private-error-detail")
        return original(path, content)

    monkeypatch.setattr(maintenance, "_atomic", flaky)
    monkeypatch.setattr(maintenance.log, "warning", lambda *a, **kw: warnings.append((a, kw)))
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(watch(tmp_path, instance, controller, interval=0.01))
        try:
            async with asyncio.timeout(5):
                while read_status(tmp_path).get("state") != "running":
                    assert not task.done()
                    await asyncio.sleep(0.01)
            assert attempts >= 3
            client = LocalClient(tmp_path, timeout=5)
            response = await asyncio.to_thread(client.request, "POST", "/maintenance/prepare")
            assert response.status_code == 200
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(warnings) == 1
    assert all(a == ("connector_activity_publish_failed",) for a, _ in warnings)
    assert "private-error-detail" not in str(warnings)
