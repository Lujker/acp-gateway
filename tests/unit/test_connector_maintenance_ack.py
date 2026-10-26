"""Controlled file-IPC failure contracts, never native OS manager operations."""

import asyncio
import json
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
            for _ in range(10):
                response = await asyncio.to_thread(
                    LocalClient(tmp_path, timeout=5).request,
                    "POST",
                    "/maintenance/prepare",
                    json={"restart_uncertain": restart_uncertain},
                )
                if response.status_code != 409:
                    break
                # A concurrent STATUS replacement can fail the inode check. Retry
                # only this pre-admission refusal, never an applied/unknown request.
                assert response.json()["detail"] == "Live connector activity is unavailable"
                assert applied == []
                assert not (tmp_path / maintenance.REQUEST).exists()
                await asyncio.sleep(0.01)
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

    def slow(writer):
        def write(path, content):
            if path.name == maintenance.STATUS:
                clock.value += 6
            return writer(path, content)

        return write

    monkeypatch.setattr(maintenance, "time", SimpleNamespace(time=lambda: clock.value))
    # Both the verified and the plain status writer are slow here.
    monkeypatch.setattr(maintenance, "_atomic", slow(maintenance._atomic))
    monkeypatch.setattr(maintenance, "_write_status", slow(maintenance._write_status))
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
            assert release.wait(15), "owned ACL publication barrier was not released"

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
            for _ in range(10):
                client_job = asyncio.create_task(
                    asyncio.to_thread(
                        LocalClient(tmp_path, timeout=0.5).request, "POST", "/maintenance/prepare"
                    )
                )
                async with asyncio.timeout(10):
                    while not entered.is_set() and not client_job.done():
                        await asyncio.sleep(0.01)
                if entered.is_set():
                    break
                # A concurrent STATUS replacement can fail the inode check. Retry
                # only this pre-admission refusal, never an applied/unknown request.
                response = await client_job
                assert response.status_code == 409
                assert response.json()["detail"] == "Live connector activity is unavailable"
                assert applies == []
                assert not (tmp_path / maintenance.REQUEST).exists()
            assert entered.is_set()
            assert controller.status()["reserved"]
            with pytest.raises(ValueError, match="timed out; inspect status"):
                await client_job
            assert applies == ["prepare"]
            assert not (tmp_path / maintenance.RESPONSE).exists()
            release.set()
            async with asyncio.timeout(10):
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


@pytest.fixture
async def ack_watcher(tmp_path, monkeypatch, controller):
    """Step real file IPC one completed watcher iteration at a time."""
    clock = SimpleNamespace(value=1000.0)
    counts = dict.fromkeys(maintenance.FIELDS, 0)
    controller.managers["hermes"] = SimpleNamespace(
        maintenance_status=lambda: counts,
        sessions=lambda: [],
        journal=lambda handle: {"handle": handle},
        retire=lambda handle: {"retired": handle},
    )
    applies, replies = [], []
    state = SimpleNamespace(fail=True, clock=clock, applies=applies, replies=replies)
    atomic = maintenance._atomic
    apply, session_control = controller.apply, controller.session_control
    ticks, proceed = asyncio.Queue(), asyncio.Semaphore(0)

    def record_apply(*args, **kwargs):
        applies.append((args, kwargs))
        return apply(*args, **kwargs)

    def record_session(*args, **kwargs):
        applies.append((args, kwargs))
        return session_control(*args, **kwargs)

    def response_writer(path, content):
        if path.name == maintenance.RESPONSE:
            replies.append(json.loads(content))
            if state.fail:
                raise PermissionError("injected ACK failure")
        return atomic(path, content)

    async def tick(_interval):
        ticks.put_nowait(None)
        await proceed.acquire()

    async def cycle(request):
        atomic(tmp_path / maintenance.REQUEST, json.dumps(request).encode())
        proceed.release()
        async with asyncio.timeout(3):
            await ticks.get()

    monkeypatch.setattr(controller, "apply", record_apply)
    monkeypatch.setattr(controller, "session_control", record_session)
    monkeypatch.setattr(maintenance, "_atomic", response_writer)
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(time=lambda: clock.value))
    monkeypatch.setattr(
        maintenance, "asyncio", SimpleNamespace(**(vars(maintenance.asyncio) | {"sleep": tick}))
    )
    task = asyncio.create_task(maintenance.watch(tmp_path, "a" * 32, controller))
    try:
        async with asyncio.timeout(3):
            await ticks.get()
        state.cycle = cycle
        state.request = {
            "version": 1,
            "instance": "a" * 32,
            "request": "1" * 32,
            "operation": "prepare",
            "expires": 1010.0,
        }
        yield state
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("operation", "change"),
    [
        ("prepare", {"operation": "resume"}),
        ("prepare", {"token": "2" * 32}),
        ("prepare", {"force": True}),
        ("prepare", {"restart_uncertain": True}),
        ("journal", {"agent": "other"}),
        ("journal", {"handle": "3" * 32}),
        ("prepare", {"expires": 1020.0}),
    ],
    ids=["operation", "token", "force", "restart-uncertain", "agent", "handle", "expiry"],
)
async def test_pending_ack_requires_exact_effective_identity(ack_watcher, operation, change):
    watcher = ack_watcher
    request = watcher.request | {"operation": operation, "agent": "hermes", "handle": "2" * 32}
    await watcher.cycle(request)
    assert len(watcher.applies) == len(watcher.replies) == 1
    assert watcher.replies[0]["status"] == 200

    watcher.fail = False
    await watcher.cycle(request | change)
    assert len(watcher.replies) == 1, "mutated request received the cached ACK"
    assert len(watcher.applies) == 1

    # The original, still-valid request can recover its ACK; omitted defaults
    # and unrelated wire fields do not change the effective controller call.
    retry = request | {"force": False, "restart_uncertain": False, "ignored": "metadata"}
    await watcher.cycle(retry)
    assert watcher.replies == [watcher.replies[0], watcher.replies[0]]
    assert len(watcher.applies) == 1
    await watcher.cycle(retry)
    assert len(watcher.replies) == 2
    assert len(watcher.applies) == 1


async def test_pending_ack_cannot_be_revived_after_original_expiry(ack_watcher):
    watcher = ack_watcher
    await watcher.cycle(watcher.request)
    assert len(watcher.replies) == len(watcher.applies) == 1
    watcher.fail = False
    watcher.clock.value = watcher.request["expires"] + 1
    await watcher.cycle(watcher.request)
    await watcher.cycle(watcher.request | {"expires": watcher.clock.value + 10})
    assert len(watcher.replies) == len(watcher.applies) == 1


@pytest.mark.parametrize("first_write_fails", [False, True])
async def test_processed_id_tombstone_survives_intervening_request(ack_watcher, first_write_fails):
    watcher = ack_watcher
    watcher.fail = first_write_fails
    await watcher.cycle(watcher.request)
    assert len(watcher.applies) == 1
    assert watcher.replies[0]["status"] == 200
    watcher.fail = False
    await watcher.cycle(
        watcher.request | {"request": "2" * 32, "operation": "resume", "force": True}
    )
    assert len(watcher.replies) == len(watcher.applies) == 2
    assert watcher.replies[-1]["status"] == 200
    await watcher.cycle(watcher.request | {"operation": "resume", "force": True})
    assert len(watcher.applies) == 2, "processed ID reapplied a mutated operation"
    assert len(watcher.replies) == 2
    await watcher.cycle(watcher.request)
    assert len(watcher.applies) == len(watcher.replies) == 2


async def test_pending_ack_distinguishes_json_argument_types(ack_watcher):
    watcher = ack_watcher
    request = watcher.request | {"token": True}
    await watcher.cycle(request)
    assert watcher.replies[0]["status"] == 200
    watcher.fail = False
    await watcher.cycle(request | {"token": 1})
    assert len(watcher.replies) == len(watcher.applies) == 1
