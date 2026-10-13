"""Admission reservation races across channels, without stopping external agents."""

import asyncio

import pytest
from test_gateway_core import CLI, SECRET, TG, make_core

from acp_gateway.core import MaintenanceBlocked
from fakes.fake_goose import FakeGooseServer


@pytest.fixture
def server():
    with FakeGooseServer(SECRET) as instance:
        yield instance


async def test_reservation_blocks_all_new_work_and_keeps_selection(server, tmp_path):
    core = make_core(server.url("ws"), tmp_path)
    await core.start()
    try:
        session = await core.new_session(TG)
        token = core.prepare_maintenance()
        for conversation in (CLI, TG):
            with pytest.raises(MaintenanceBlocked, match="reserved"):
                await core.new_session(conversation)
            with pytest.raises(MaintenanceBlocked, match="reserved"):
                await core.submit(conversation, "do not run")
        assert core.active_session(TG).id == session.id
        assert not core.running_jobs()
        with pytest.raises(MaintenanceBlocked):
            core.resume_maintenance("é")
        core.resume_maintenance(token)
        job = await core.ask(TG, "hello")
        assert job.session_id == session.id
    finally:
        await core.close()


@pytest.mark.parametrize("operation", ["new", "submit"])
async def test_inflight_creation_refuses_reservation_and_releases_counter(
    server, tmp_path, monkeypatch, operation
):
    core = make_core(server.url("ws"), tmp_path)
    await core.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = core.agent("work").new_session

    async def delayed(cwd):
        entered.set()
        await release.wait()
        return await original(cwd)

    monkeypatch.setattr(core.agent("work"), "new_session", delayed)
    task = asyncio.create_task(
        core.new_session(CLI) if operation == "new" else core.submit(CLI, "hi")
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(MaintenanceBlocked, match="session creation"):
            core.prepare_maintenance()
        assert not core.maintenance_status()["reserved"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert core.maintenance_status()["creating_sessions"] == 0
        core.resume_maintenance(core.prepare_maintenance())
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await core.close()


@pytest.mark.parametrize("operation", ["submit", "new"])
async def test_request_queued_on_conversation_lock_cannot_bypass_reservation(
    server, tmp_path, operation
):
    core = make_core(server.url("ws"), tmp_path)
    await core.start()
    lock = core._locks[CLI]
    await lock.acquire()
    task = asyncio.create_task(
        core.submit(CLI, "must not run") if operation == "submit" else core.new_session(CLI)
    )
    try:
        await asyncio.sleep(0)
        token = core.prepare_maintenance()
        lock.release()
        with pytest.raises(MaintenanceBlocked):
            await task
        assert not core.store.sessions(CLI)
        core.resume_maintenance(token)
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(task, return_exceptions=True)
        await core.close()


async def test_approval_is_not_settled_or_replayed_by_maintenance(server, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def human(request):
        entered.set()
        await release.wait()
        return request.option("reject_once").option_id

    core = make_core(server.url("ws"), tmp_path, handler=human)
    await core.start()
    try:
        job = await core.submit(CLI, "run exactly: echo test .")
        await asyncio.wait_for(entered.wait(), 5)
        assert core.maintenance_status()["pending_approvals"] == 1
        with pytest.raises(MaintenanceBlocked):
            core.prepare_maintenance()
        assert core.maintenance_status()["pending_approvals"] == 1
        release.set()
        result = await core.wait(job.id, 5)
        assert result.answer == "DENIED"
        assert core.maintenance_status()["pending_approvals"] == 0
        core.resume_maintenance(core.prepare_maintenance())
    finally:
        release.set()
        await core.close()


async def test_running_turn_refuses_maintenance_without_cancelling(server, tmp_path, monkeypatch):
    core = make_core(server.url("ws"), tmp_path)
    await core.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = core.agent("work").prompt

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        async for event in original(*args, **kwargs):
            yield event

    monkeypatch.setattr(core.agent("work"), "prompt", delayed)
    try:
        job = await core.submit(CLI, "hi")
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(MaintenanceBlocked, match="active jobs"):
            core.prepare_maintenance()
        assert core.job(job.id).status.value == "running"
        release.set()
        await core.wait(job.id, 5)
        core.resume_maintenance(core.prepare_maintenance())
    finally:
        release.set()
        await core.close()
