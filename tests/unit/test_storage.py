"""SQLite store: migrations, session mapping, jobs."""

import multiprocessing
import os
import sqlite3
from datetime import timedelta
from importlib import resources

import pytest

from acp_gateway.storage import Conversation, JobStatus, Store, db
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


def test_upgrade_from_initial_schema_preserves_existing_sessions_and_jobs(tmp_path):
    path = tmp_path / "gateway.db"
    conn = sqlite3.connect(path, isolation_level=None)
    script = resources.files("acp_gateway.storage.migrations").joinpath("0001_initial.sql")
    conn.executescript(script.read_text() + "\nPRAGMA user_version = 1;")
    old = Store(conn)
    # Use the existing SQL schema; only read helpers need Row objects.
    conn.row_factory = sqlite3.Row
    session = old.add_session(CLI, "old-session", "/work")
    old.add_job("old-job", session.id)
    old.close()
    upgraded = Store.open(path)
    try:
        assert upgraded.schema_version == 4
        assert upgraded.active_session(CLI).acp_session_id == "old-session"
        assert upgraded.job("old-job").status is JobStatus.RUNNING
        assert upgraded.approval_audit() == []
    finally:
        upgraded.close()


LATEST = max(version for version, _ in db._migrations())


def _open_concurrently(paths, barrier, results):
    """Child process: open each database at the same moment as the other children."""
    for path in paths:
        barrier.wait(timeout=30)
        try:
            store = Store.open(path)
            results.put(("ok", store.schema_version))
            store.close()
        except Exception as exc:  # reported to the parent
            results.put(("error", repr(exc)))


def _initial_schema(path):
    conn = sqlite3.connect(path)
    script = resources.files("acp_gateway.storage.migrations").joinpath("0001_initial.sql")
    conn.executescript(script.read_text() + "\nPRAGMA user_version = 1;")
    conn.close()


def test_concurrent_open_migrates_once(tmp_path):
    """A daemon restart racing `acpgw computers enroll` must not fail or double-migrate."""
    paths = [tmp_path / f"fresh-{n}" / "gateway.db" for n in range(3)]
    for n in range(2):  # pending migrations on an existing database
        pending = tmp_path / f"pending-{n}" / "gateway.db"
        pending.parent.mkdir()
        _initial_schema(pending)
        paths.append(pending)
    workers = 4
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(workers)
    results = context.Queue()
    processes = [
        context.Process(target=_open_concurrently, args=(paths, barrier, results))
        for _ in range(workers)
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=60) for _ in range(workers * len(paths))]
    for process in processes:
        process.join(timeout=30)
    assert outcomes == [("ok", LATEST)] * len(outcomes)
    for path in paths:
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.close()


def test_failing_migration_rolls_back_fully(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    Store.open(path).close()
    real = db._migrations
    broken = "CREATE TABLE half_done (a);\nCREATE TABLE half_done (a);\n"
    monkeypatch.setattr(db, "_migrations", lambda: [*real(), (LATEST + 1, broken)])
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone() is None
        )
    finally:
        conn.close()


def test_migration_statements_may_contain_semicolons(tmp_path, monkeypatch):
    real = db._migrations
    script = (
        "-- a comment; with a semicolon\n"
        "CREATE TABLE notes (body TEXT DEFAULT 'a;b');\n"
        "CREATE TABLE note_log (body TEXT);\n"
        "CREATE TRIGGER notes_log AFTER INSERT ON notes BEGIN\n"
        "    INSERT INTO note_log (body) VALUES (new.body || ';');\n"
        "END;\n"
        "-- trailing comment\n"
    )
    monkeypatch.setattr(db, "_migrations", lambda: [*real(), (LATEST + 1, script)])
    store = Store.open(tmp_path / "gateway.db")
    try:
        assert store.schema_version == LATEST + 1
        store._conn.execute("INSERT INTO notes DEFAULT VALUES")
        assert store._conn.execute("SELECT body FROM note_log").fetchone()[0] == "a;b;"
    finally:
        store.close()


def test_database_from_newer_release_is_refused(tmp_path):
    path = tmp_path / "gateway.db"
    Store.open(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {LATEST + 1}")
    conn.close()
    with pytest.raises(db.SchemaTooNewError, match="newer than this acpgw supports") as error:
        Store.open(path)
    assert isinstance(error.value, ValueError)  # the CLI reports ValueError as a clean error


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_database_file_is_private(tmp_path):
    path = tmp_path / "data" / "gateway.db"
    Store.open(path).close()
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_existing_database_and_wal_files_become_private(tmp_path):
    path = tmp_path / "gateway.db"
    first = Store.open(path)
    files = [path, tmp_path / "gateway.db-wal", tmp_path / "gateway.db-shm"]
    try:
        for file in files:
            assert file.is_file()
            file.chmod(0o644)
        second = Store.open(path)
        try:
            assert all(file.stat().st_mode & 0o077 == 0 for file in files)
        finally:
            second.close()
    finally:
        first.close()


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
