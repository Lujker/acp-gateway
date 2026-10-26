import asyncio
import json
import subprocess
import sys
import threading
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


@pytest.fixture(autouse=True)
def _local_ipc_acl(tmp_path, monkeypatch, request):
    """Behavior seam only: real file IPC, no PowerShell or OS-manager calls.

    Clone the atomic writer's import scope rather than modifying shared modules.
    The directory helper keeps its real traversal and calls a fixture-only checker.
    Native DACL policy is covered separately in test_windows_acl.py.
    """
    import builtins
    from pathlib import Path
    from types import FunctionType

    from acp_gateway import windows_acl
    from acp_gateway.cli import connector_maintenance, wizard
    from acp_gateway.connectors import maintenance

    root = tmp_path.resolve(strict=True)
    calls = []
    seam = SimpleNamespace(calls=calls, before_check=lambda: None)

    def check(files, *, directories=(), purpose=None):
        seam.before_check()
        for path in (*files, *directories):
            path = Path(path)
            assert path.is_absolute() and path.resolve(strict=True).is_relative_to(root)
            for part in (path, *path.parents):
                if part == root.parent:
                    break
                info = part.lstat()
                assert not part.is_symlink()
                assert not getattr(info, "st_file_attributes", 0) & 0x400
            calls.append(path.name)
        return None

    ensure = FunctionType(
        windows_acl.ensure_private_directory.__code__,
        {**windows_acl.ensure_private_directory.__globals__, "check_private_paths": check},
    )
    acl = SimpleNamespace(check_private_paths=check, ensure_private_directory=ensure)

    def local_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "acp_gateway.windows_acl" and level == 0:
            return acl
        return builtins.__import__(name, globals, locals, fromlist, level)

    atomic = FunctionType(
        wizard._atomic.__code__,
        {
            **wizard._atomic.__globals__,
            "sys": SimpleNamespace(platform="win32"),
            "__builtins__": {**vars(builtins), "__import__": local_import},
        },
    )

    async def drained_to_thread(function, /, *args, **kwargs):
        job = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            # The OS write must finish before watcher cleanup removes STATUS.
            # Its own error (e.g. a missing request file) must not replace the
            # cancellation: the watcher ignores OSError and would never stop.
            await asyncio.gather(job, return_exceptions=True)
            raise

    monkeypatch.setattr(
        maintenance,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"to_thread": drained_to_thread})),
    )
    if getattr(request, "param", "behavior") == "native":
        original = subprocess.run

        def forbidden(argv, **kwargs):
            assert all("powershell" not in arg.lower() for arg in argv)
            return original(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", forbidden)
        yield None
        return
    monkeypatch.setattr(maintenance, "_atomic", atomic)
    monkeypatch.setattr(connector_maintenance, "_atomic", atomic)
    yield seam


@pytest.fixture
def controller():
    return ConnectorMaintenance([SimpleNamespace(alias="hermes", backend="hermes")], {})


@pytest.mark.parametrize(
    "_local_ipc_acl",
    [
        "behavior",
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "win32", reason="real Windows ACL maintenance acceptance"
            ),
        ),
    ],
    indirect=True,
)
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
            published = json.loads((tmp_path / STATUS).read_bytes())["published"]
            assert 0 <= time.time() - published < 5
            print(f"maintenance status age={time.time() - published:.3f}s")
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


@pytest.mark.parametrize(
    "_local_ipc_acl",
    [
        "behavior",
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "win32", reason="real Windows ACL maintenance acceptance"
            ),
        ),
    ],
    indirect=True,
)
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
            published = json.loads((tmp_path / STATUS).read_bytes())["published"]
            assert 0 <= time.time() - published < 5
            print(f"maintenance status age={time.time() - published:.3f}s")
            assert controller.status()["reserved"] and counts["uncertain_sessions"] == 1
            with pytest.raises(Exception, match="reserved"):
                controller.check_admission()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "_local_ipc_acl",
    [
        "behavior",
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "win32", reason="real Windows ACL maintenance acceptance"
            ),
        ),
    ],
    indirect=True,
)
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
            published = json.loads((tmp_path / STATUS).read_bytes())["published"]
            assert 0 <= time.time() - published < 5
            print(f"maintenance status age={time.time() - published:.3f}s")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(warnings) == 1
    assert all(a == ("connector_activity_publish_failed",) for a, _ in warnings)
    assert "private-error-detail" not in str(warnings)


async def test_activity_reverifies_acl_periodically_not_on_every_write(
    tmp_path, controller, monkeypatch
):
    from acp_gateway.connectors import maintenance

    original = maintenance._atomic
    clock = SimpleNamespace(value=100.0)
    checked = []
    failures = 1

    def atomic(path, content):
        nonlocal failures
        if path.name in {STATUS, ".connector-activity-acl-check"}:
            checked.append(clock.value)
            if failures:
                failures -= 1
                raise ValueError("Windows discovery ACL verification timed out")
        return original(path, content)

    monkeypatch.setattr(maintenance, "_atomic", atomic)
    monkeypatch.setattr(maintenance, "monotonic", lambda: clock.value)
    monkeypatch.setattr(maintenance.log, "warning", lambda *a, **kw: None)
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(watch(tmp_path, instance, controller, interval=0.01))
        try:
            async with asyncio.timeout(5):
                while read_status(tmp_path).get("state") != "running":
                    assert not task.done()
                    await asyncio.sleep(0.01)
                # A failed ACL check is retried instead of ending the watcher.
                assert checked == [100.0, 100.0]
                first = json.loads((tmp_path / STATUS).read_bytes())["published"]
                while json.loads((tmp_path / STATUS).read_bytes())["published"] == first:
                    await asyncio.sleep(0.01)
                assert checked == [100.0, 100.0]
                clock.value += maintenance.ACL_RECHECK_SECONDS
                while len(checked) < 3:
                    assert not task.done()
                    await asyncio.sleep(0.01)
                assert checked[2] == clock.value
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_slow_periodic_acl_check_does_not_interrupt_fresh_status(
    tmp_path, controller, monkeypatch
):
    from acp_gateway.connectors import maintenance

    entered, release = threading.Event(), threading.Event()
    clock = SimpleNamespace(value=100.0)
    original = maintenance._atomic

    def check(path, content):
        if clock.value > 100 and path.name in {STATUS, ".connector-activity-acl-check"}:
            entered.set()
            if not release.wait(5):
                raise ValueError("test check timeout")
        return original(path, content)

    monkeypatch.setattr(maintenance, "_atomic", check)
    monkeypatch.setattr(maintenance, "monotonic", lambda: clock.value)
    with FileLock(tmp_path / "connector.lock"), control_record(tmp_path) as instance:
        task = asyncio.create_task(watch(tmp_path, instance, controller, interval=0.01))
        try:
            async with asyncio.timeout(3):
                while read_status(tmp_path).get("state") != "running":
                    await asyncio.sleep(0.01)
            clock.value += maintenance.ACL_RECHECK_SECONDS / 2
            assert await asyncio.to_thread(entered.wait, 1)
            first = json.loads((tmp_path / STATUS).read_bytes())["published"]
            async with asyncio.timeout(1):
                for _ in range(2):
                    while json.loads((tmp_path / STATUS).read_bytes())["published"] == first:
                        await asyncio.sleep(0.01)
                    first = json.loads((tmp_path / STATUS).read_bytes())["published"]
            assert read_status(tmp_path)["state"] == "running"
            assert not task.done()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
