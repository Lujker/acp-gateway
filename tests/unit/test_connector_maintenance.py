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
