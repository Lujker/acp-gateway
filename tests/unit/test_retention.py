"""Retention boundaries, lifecycle and failure recovery."""

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from acp_gateway.config import RetentionSettings, Settings
from acp_gateway.core import GatewayCore
from acp_gateway.storage import Conversation, JobStatus, Store
from acp_gateway.storage.records import ApprovalAudit, utcnow

CONVERSATION = Conversation("cli", "default", "work")


def audit(store, name, at, job_id=None):
    store.record_approval(
        ApprovalAudit(
            id=name,
            job_id=job_id,
            conversation=CONVERSATION,
            acp_session_id="session",
            tool_call_id="tool",
            title=None,
            kind=None,
            raw_input=None,
            requested_at=at,
            resolved_at=at,
            outcome="denied",
        )
    )


def test_audit_batch_boundary_and_job_independence():
    store = Store.open(":memory:")
    try:
        now = utcnow()
        session = store.add_session(CONVERSATION, "session", "/work")
        store.add_job("finished", session.id)
        store.finish_job("finished", JobStatus.COMPLETED)
        store.add_job("running", session.id)
        for name in ("old-a", "old-b", "old-c"):
            audit(store, name, now - timedelta(days=10), "finished")
        audit(store, "boundary", now - timedelta(days=7))
        audit(store, "recent", now)
        assert store.prune_jobs(now + timedelta(seconds=1), limit=1) == 1
        assert store.job("running") is not None
        assert store.session(session.id) is not None
        assert len(store.approval_audit()) == 5
        assert all(row.job_id is None for row in store.approval_audit())
        cutoff = now - timedelta(days=7)
        assert store.prune_approval_audit(cutoff, limit=2) == 2
        assert store.prune_approval_audit(cutoff, limit=2) == 1
        assert store.prune_approval_audit(cutoff, limit=2) == 0
        assert {row.id for row in store.approval_audit()} == {"boundary", "recent"}
    finally:
        store.close()


@pytest.mark.parametrize("limit", [0, -1, 10001, True, 1.5])
def test_invalid_batches_cannot_expand_delete(limit):
    store = Store.open(":memory:")
    try:
        audit(store, "old", utcnow() - timedelta(days=10))
        with pytest.raises(ValueError):
            store.prune_approval_audit(utcnow(), limit=limit)
        with pytest.raises(ValueError):
            store.prune_jobs(utcnow(), limit=limit)
        assert len(store.approval_audit()) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "values",
    [
        {"jobs_days": 0},
        {"approval_audit_days": 0},
        {"sweep_interval_seconds": 59},
        {"batch_size": 10001},
    ],
)
def test_retention_configuration_bounds(values):
    with pytest.raises(ValidationError):
        RetentionSettings(**values)


def test_nested_retention_environment(monkeypatch):
    monkeypatch.setenv("ACPGW_RETENTION__APPROVAL_AUDIT_DAYS", "90")
    assert Settings().retention.approval_audit_days == 90


@pytest.mark.asyncio
async def test_default_keeps_audit_and_periodic_task_stops():
    store = Store.open(":memory:")
    audit(store, "old", utcnow() - timedelta(days=1000))
    core = GatewayCore(store, {})
    await core.start()
    task = core._retention_task
    try:
        assert task is not None and not task.done()
        assert len(store.approval_audit()) == 1
    finally:
        await core.close()
    assert task.done()
    await core.close()


@pytest.mark.asyncio
async def test_periodic_expiry_without_restart_and_retry(monkeypatch):
    store = Store.open(":memory:")
    audit(store, "old", utcnow() - timedelta(days=10))
    audit(store, "recent", utcnow())
    retention = RetentionSettings(approval_audit_days=7).model_copy(
        update={"sweep_interval_seconds": 0.01}
    )
    core = GatewayCore(store, {}, retention=retention)
    original = store.prune_approval_audit
    cleaned = asyncio.Event()
    calls = 0

    def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database failure")
        result = original(*args, **kwargs)
        cleaned.set()
        return result

    monkeypatch.setattr(store, "prune_approval_audit", flaky)
    try:
        await core.start()
        assert len(store.approval_audit()) == 2
        await asyncio.wait_for(cleaned.wait(), timeout=2)
        assert calls >= 2
        assert [row.id for row in store.approval_audit()] == ["recent"]
        # A row added after startup is collected by a later scheduled sweep.
        cleaned.clear()
        audit(store, "another-old", utcnow() - timedelta(days=10))
        await asyncio.wait_for(cleaned.wait(), timeout=2)
        assert [row.id for row in store.approval_audit()] == ["recent"]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_startup_and_scheduled_sweeps_respect_batch_size():
    store = Store.open(":memory:")
    now = utcnow()
    for name in ("a", "b", "c"):
        audit(store, name, now - timedelta(days=10))
    core = GatewayCore(store, {}, retention=RetentionSettings(approval_audit_days=7, batch_size=1))
    try:
        await core.start()
        assert len(store.approval_audit()) == 2
        core._sweep_retention()
        assert len(store.approval_audit()) == 1
    finally:
        await core.close()
