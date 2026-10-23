"""Controlled file-IPC failure contracts, never native OS manager operations."""

import asyncio
import threading
from types import FunctionType, SimpleNamespace

import pytest
from filelock import FileLock
from test_connector_maintenance import _local_ipc_acl as _local_ipc_acl
from test_connector_maintenance import controller as controller

from acp_gateway.cli.connector_maintenance import LocalClient
from acp_gateway.connectors import maintenance
from acp_gateway.connectors.lifecycle import control_record


@pytest.mark.parametrize("restart_uncertain", [False, True])
async def test_response_publication_failure_retries_ack_without_reapplying(
    tmp_path, monkeypatch, controller, restart_uncertain
):
    counts = dict.fromkeys(maintenance.FIELDS, 0)
    counts["uncertain_sessions"] = int(restart_uncertain)
    controller.managers["hermes"] = SimpleNamespace(maintenance_status=lambda: counts)
    atomic = maintenance._atomic
    apply = controller.apply
    applied = []
    attempts = 0

    def apply_once(*args, **kwargs):
        applied.append(args[0])
        return apply(*args, **kwargs)

    class ResponseReplacePath(type(tmp_path)):
        def replace(self, target):
            nonlocal attempts
            if target.name == maintenance.RESPONSE:
                attempts += 1
                if attempts == 1:
                    raise PermissionError("injected response replace failure")
            return super().replace(target)

    # Keep mkdir, ACL checks, mkstemp, write/fsync and finally cleanup real;
    # inject only replacement failure in this atomic function's local globals.
    failed_writer = FunctionType(
        atomic.__code__, {**atomic.__globals__, "Path": ResponseReplacePath}
    )
    monkeypatch.setattr(controller, "apply", apply_once)
    monkeypatch.setattr(maintenance, "_atomic", failed_writer)
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(maintenance.watch(tmp_path, instance, controller, interval=0.01))
        try:
            async with asyncio.timeout(3):
                while maintenance.read_status(tmp_path)["state"] != "running":
                    await asyncio.sleep(0.01)
            response = await asyncio.to_thread(
                LocalClient(tmp_path, timeout=1).request,
                "POST",
                "/maintenance/prepare",
                json={"restart_uncertain": restart_uncertain},
            )
            assert response.status_code == 200
            assert response.json()["token"] == controller.token
            assert applied == ["prepare"]
            assert attempts == 2
            assert controller.status()["reserved"]
            assert counts["uncertain_sessions"] == int(restart_uncertain)
            assert controller.token not in (tmp_path / maintenance.STATUS).read_text()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_slow_status_publication_remains_fail_closed(tmp_path, monkeypatch, controller):
    """Known latency risk: keep the five-second freshness policy unchanged."""
    clock = SimpleNamespace(value=1000.0)
    original = maintenance._atomic

    def slow_status(path, content):
        if path.name == maintenance.STATUS:
            clock.value += 6
        return original(path, content)

    monkeypatch.setattr(maintenance, "time", SimpleNamespace(time=lambda: clock.value))
    monkeypatch.setattr(maintenance, "_atomic", slow_status)
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(maintenance.watch(tmp_path, instance, controller, interval=0.01))
        try:
            async with asyncio.timeout(3):
                while not (tmp_path / maintenance.STATUS).exists():
                    await asyncio.sleep(0.01)
            assert maintenance.read_status(tmp_path)["state"] == "unknown"
            response = await asyncio.to_thread(
                LocalClient(tmp_path, timeout=1).request, "POST", "/maintenance/prepare"
            )
            assert response.status_code == 409
            assert not controller.status()["reserved"]
            assert not (tmp_path / maintenance.REQUEST).exists()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_acl_publication_barrier_documents_uncertain_timeout(
    tmp_path, monkeypatch, controller, _local_ipc_acl
):
    """Bounded delayed ACK can still leave admission reserved after client timeout."""
    entered, release = threading.Event(), threading.Event()
    target = threading.local()
    original = maintenance._atomic
    applies = []
    apply = controller.apply

    def record_apply(*args, **kwargs):
        applies.append(args[0])
        return apply(*args, **kwargs)

    def acl_barrier():
        if getattr(target, "response", False):
            entered.set()
            assert release.wait(3), "owned ACL publication barrier was not released"

    def atomic(path, content):
        target.response = path.name == maintenance.RESPONSE
        try:
            return original(path, content)
        finally:
            target.response = False

    _local_ipc_acl.before_check = acl_barrier
    monkeypatch.setattr(controller, "apply", record_apply)
    monkeypatch.setattr(maintenance, "_atomic", atomic)
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(maintenance.watch(tmp_path, instance, controller, interval=0.01))
        client_job = None
        try:
            async with asyncio.timeout(3):
                while maintenance.read_status(tmp_path)["state"] != "running":
                    await asyncio.sleep(0.01)
            client_job = asyncio.create_task(
                asyncio.to_thread(
                    LocalClient(tmp_path, timeout=0.5).request, "POST", "/maintenance/prepare"
                )
            )
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            assert controller.status()["reserved"]
            with pytest.raises(ValueError, match="timed out; inspect status"):
                await client_job
            assert applies == ["prepare"]
            assert not (tmp_path / maintenance.RESPONSE).exists()
            release.set()
            async with asyncio.timeout(3):
                while not (tmp_path / maintenance.RESPONSE).exists():
                    await asyncio.sleep(0.01)
            assert controller.status()["reserved"]
            assert applies == ["prepare"]
        finally:
            release.set()
            if client_job is not None:
                await asyncio.gather(client_job, return_exceptions=True)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
