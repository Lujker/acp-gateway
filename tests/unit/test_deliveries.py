"""Outbound readiness and cursor transitions survive reopening SQLite."""

import sqlite3
from datetime import timedelta

import pytest

from acp_gateway.storage import Conversation, JobStatus, Store
from acp_gateway.storage.records import utcnow

NAMESPACE = "telegram:123456"


@pytest.fixture
def store(tmp_path):
    instance = Store.open(tmp_path / "gateway.db")
    yield instance
    instance.close()


def queued(store, name="job"):
    session = store.add_session(Conversation("telegram", "12345", "work"), name, "/work")
    return store.add_job(name, session.id, delivery_namespace=NAMESPACE)


def test_job_and_delivery_admission_are_atomic(store):
    session = store.add_session(Conversation("telegram", "12345", "work"), "s", "/work")
    store._conn.execute(
        "CREATE TRIGGER deny_delivery BEFORE INSERT ON outbound_deliveries "
        "BEGIN SELECT RAISE(ABORT, 'full'); END"
    )
    with pytest.raises(sqlite3.Error):
        store.add_job("job", session.id, delivery_namespace=NAMESPACE)
    assert store.job("job") is None


def test_finish_and_delivery_readiness_are_atomic(store):
    queued(store)
    store._conn.execute(
        "CREATE TRIGGER deny_ready BEFORE UPDATE ON outbound_deliveries "
        "BEGIN SELECT RAISE(ABORT, 'full'); END"
    )
    with pytest.raises(sqlite3.Error):
        store.finish_job("job", JobStatus.COMPLETED, answer="keep")
    assert store.job("job").status is JobStatus.RUNNING
    assert store.deliveries.get(NAMESPACE, "job")["state"] == "waiting"


def test_pending_acknowledged_and_ambiguous_chunks_survive_restart(store, tmp_path):
    queued(store)
    assert store.deliveries.due(NAMESPACE) is None
    store.finish_job("job", JobStatus.COMPLETED, answer="x" * 5000)
    assert store.deliveries.get("telegram:another", "job") is None
    assert store.deliveries.claim(NAMESPACE, "job")
    assert not store.deliveries.claim(NAMESPACE, "job")
    store.deliveries.acknowledge(NAMESPACE, "job", last=False)
    assert store.deliveries.claim(NAMESPACE, "job")
    reopened = Store.open(tmp_path / "gateway.db")
    try:
        reopened.deliveries.recover(NAMESPACE)
        row = reopened.deliveries.get(NAMESPACE, "job")
        assert row["state"] == "unknown" and row["next_chunk"] == 1
        assert reopened.deliveries.due(NAMESPACE) is None
        assert reopened.job("job").answer == "x" * 5000
    finally:
        reopened.close()


def test_interrupted_job_becomes_deliverable_and_retention_removes_reference(store):
    queued(store)
    assert store.interrupt_running_jobs() == 1
    assert store.deliveries.due(NAMESPACE)["job_id"] == "job"
    assert store.prune_jobs(utcnow() + timedelta(days=1)) == 1
    assert store.deliveries.get(NAMESPACE, "job") is None


def test_known_rejection_is_delayed_and_delivered_is_terminal(store):
    queued(store)
    store.finish_job("job", JobStatus.COMPLETED)
    store.deliveries.claim(NAMESPACE, "job")
    store.deliveries.settle(NAMESPACE, "job", "pending", retry_after=3600)
    assert store.deliveries.due(NAMESPACE) is None
    store._conn.execute("UPDATE outbound_deliveries SET retry_at = 0")
    assert store.deliveries.due(NAMESPACE)
    store.deliveries.claim(NAMESPACE, "job")
    store.deliveries.acknowledge(NAMESPACE, "job", last=True)
    store.deliveries.recover(NAMESPACE)
    assert store.deliveries.get(NAMESPACE, "job")["state"] == "delivered"
    assert store.deliveries.due(NAMESPACE) is None
