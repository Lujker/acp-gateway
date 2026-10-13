import asyncio
import json
import os

import pytest

from acp_gateway.connectors.lifecycle import RUNTIME_FILE, STOP_FILE, control_record, watch_stop


def test_runtime_record_has_no_credentials_and_new_instance_on_restart(tmp_path):
    with control_record(tmp_path) as first:
        record = json.loads((tmp_path / RUNTIME_FILE).read_text())
        assert record == {"version": 1, "instance": first, "pid": os.getpid(), "controlId": None}
        assert len(first) == 32
        if os.name == "posix":
            assert (tmp_path / RUNTIME_FILE).stat().st_mode & 0o077 == 0
    assert not (tmp_path / RUNTIME_FILE).exists()
    with pytest.raises(RuntimeError), control_record(tmp_path) as second:
        assert second != first
        raise RuntimeError("simulated exit")
    assert not (tmp_path / RUNTIME_FILE).exists()


@pytest.mark.parametrize("initial", [None, "malformed", "oversized", "stale", "bool_version"])
async def test_only_matching_instance_stops_connector(tmp_path, initial):
    path = tmp_path / STOP_FILE
    with control_record(tmp_path) as instance:
        if initial == "malformed":
            path.write_text("{not json")
        elif initial == "oversized":
            path.write_text("x" * 1025)
        elif initial in {"stale", "bool_version"}:
            path.write_text(
                json.dumps(
                    {
                        "version": True if initial == "bool_version" else 1,
                        "instance": instance if initial == "bool_version" else "old",
                    }
                )
            )
        stopped = asyncio.Event()
        watcher = asyncio.create_task(watch_stop(tmp_path, instance, stopped.set, interval=0.01))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(stopped.wait(), 0.05)
            path.write_text(json.dumps({"version": 1, "instance": instance}))
            await asyncio.wait_for(watcher, 1)
            assert stopped.is_set()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


async def test_old_stop_request_cannot_stop_new_process(tmp_path):
    with control_record(tmp_path) as old:
        (tmp_path / STOP_FILE).write_text(json.dumps({"version": 1, "instance": old}))
    with control_record(tmp_path) as current:
        stopped = asyncio.Event()
        watcher = asyncio.create_task(watch_stop(tmp_path, current, stopped.set, interval=0.01))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(stopped.wait(), 0.05)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
