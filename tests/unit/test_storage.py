"""SQLite store: migrations, session mapping, jobs."""

import os
import sqlite3
from datetime import timedelta

import pytest

from acp_gateway.storage import Conversation, JobStatus, Store
from acp_gateway.storage.records import utcnow

CLI = Conversation("cli", "default", "work")
TG = Conversation("telegram", "12345", "work")


@pytest.fixture
def store():
    s = Store.open(":memory:")
    yield s
    s.close()


def test_migrations_are_applied_once(tmp_path):
    path = tmp_path / "data" / "gateway.db"
    first = Store.open(path)
    version = first.schema_version
    first.close()
    second = Store.open(path)  # re-running migrate() on a current schema is a no-op
    assert second.schema_version == version >= 1
    second.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_database_file_is_private(tmp_path):
    path = tmp_path / "data" / "gateway.db"
    Store.open(path).close()
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0


def test_mapping_survives_reopen(tmp_path):
    path = tmp_path / "gateway.db"
    first = Store.open(path)
    first.add_session(CLI, "20261007_1", "/work")
    first.close()

    second = Store.open(path)
    active = second.active_session(CLI)
    second.close()
    assert active is not None
    assert active.acp_session_id == "20261007_1"
    assert active.cwd == "/work"


def test_new_session_becomes_active_and_sessions_are_listed_by_last_use(store, monkeypatch):
    clock = iter(utcnow() + timedelta(minutes=i) for i in range(10))
    monkeypatch.setattr("acp_gateway.storage.db.utcnow", lambda: next(clock))
    first = store.add_session(CLI, "s1", "/w")
    second = store.add_session(CLI, "s2", "/w")
    assert store.active_session(CLI) == second
    assert [s.acp_session_id for s in store.sessions(CLI)] == ["s2", "s1"]

    store.touch_session(first.id)
    store.set_active(CLI, first.id)
    assert store.active_session(CLI).acp_session_id == "s1"
    assert [s.acp_session_id for s in store.sessions(CLI)] == ["s1", "s2"]


def test_add_session_without_activation(store):
    store.add_session(CLI, "s1", "/w")
    store.add_session(CLI, "s2", "/w", activate=False)
    assert store.active_session(CLI).acp_session_id == "s1"


def test_conversations_are_isolated(store):
    store.add_session(CLI, "s1", "/w")
    store.add_session(TG, "s2", "/w")
    assert [s.acp_session_id for s in store.sessions(CLI)] == ["s1"]
    assert store.find_session(CLI, "s2") is None
    assert store.find_session(TG, "s2") is not None
    other_agent = Conversation("cli", "default", "home")
    assert store.active_session(other_agent) is None


def test_an_agent_session_belongs_to_one_conversation(store):
    store.add_session(CLI, "s1", "/w")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_session(TG, "s1", "/w")
    assert store.active_session(TG) is None  # the failed insert left no half-state


def test_session_title(store):
    session = store.add_session(CLI, "s1", "/w")
    store.set_session_title(session.id, "Pong")
    assert store.session(session.id).title == "Pong"


def test_job_lifecycle(store):
    session = store.add_session(CLI, "s1", "/w")
    job = store.add_job("job1", session.id)
    assert job.status is JobStatus.RUNNING
    assert job.conversation == CLI
    assert job.acp_session_id == "s1"

    done = store.finish_job(
        "job1",
        JobStatus.COMPLETED,
        answer="pong",
        stop_reason="end_turn",
        usage={"totalTokens": 10},
    )
    assert done.status is JobStatus.COMPLETED
    assert done.answer == "pong"
    assert done.usage == {"totalTokens": 10}
    assert done.finished_at is not None
    assert store.job("job1") == done
    assert store.job("missing") is None


def test_running_jobs_are_interrupted_after_restart(store):
    session = store.add_session(CLI, "s1", "/w")
    store.add_job("left", session.id)
    store.add_job("done", session.id)
    store.finish_job("done", JobStatus.COMPLETED, answer="x")

    assert store.interrupt_running_jobs() == 1
    left = store.job("left")
    assert left.status is JobStatus.INTERRUPTED
    assert "restarted" in left.error
    assert store.job("done").status is JobStatus.COMPLETED


def test_prune_removes_only_old_finished_jobs(store):
    session = store.add_session(CLI, "s1", "/w")
    store.add_job("old", session.id)
    store.finish_job("old", JobStatus.COMPLETED)
    store.add_job("running", session.id)

    assert store.prune_jobs(utcnow() - timedelta(days=1)) == 0
    assert store.prune_jobs(utcnow() + timedelta(seconds=1)) == 1
    assert store.job("old") is None
    assert store.job("running") is not None


@pytest.mark.parametrize(
    ("channel", "key", "agent"),
    [("Telegram", "1", "work"), ("cli", "", "work"), ("cli", "x" * 257, "work"), ("cli", "1", "")],
)
def test_conversation_validation(channel, key, agent):
    with pytest.raises(ValueError):
        Conversation(channel, key, agent)


def test_conversation_str():
    assert str(TG) == "telegram:12345@work"
