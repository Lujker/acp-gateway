import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from acp_gateway import maintenance_worker as worker


@pytest.fixture
def plan(tmp_path):
    prefix = tmp_path / "tools/acp-gateway"
    prefix.mkdir(parents=True)
    (prefix / "version").write_text("old")
    entry = tmp_path / "bin/acpgw"
    entry.parent.mkdir()
    entry.write_text("old-entry")
    database = tmp_path / "gateway.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE memory (word TEXT)")
        connection.execute("INSERT INTO memory VALUES ('AMBER')")
    return {
        "operation": "update",
        "pid": os.getpid(),
        "lease": str(tmp_path / "installation.lock"),
        "prefix": str(prefix),
        "entry": str(entry),
        "database": str(database),
        "command": ["uv", "tool", "install"],
    }


@pytest.mark.parametrize("failure", ["installer", "health"])
def test_failed_update_restores_program_entry_and_database(plan, tmp_path, monkeypatch, failure):
    def mutate(command, **kwargs):
        (Path(plan["prefix"]) / "version").write_text("new")
        Path(plan["entry"]).write_text("new-entry")
        with closing(sqlite3.connect(plan["database"])) as connection, connection:
            connection.execute("UPDATE memory SET word='COBALT'")
        if failure == "installer":
            raise ValueError("installer failed after partial mutation")

    monkeypatch.setattr(worker, "run", mutate)
    monkeypatch.setattr(
        worker, "health", lambda plan: (_ for _ in ()).throw(ValueError("bad health"))
    )
    assert worker.execute(plan, tmp_path / "backup") == 1
    assert (Path(plan["prefix"]) / "version").read_text() == "old"
    assert Path(plan["entry"]).read_text() == "old-entry"
    with closing(sqlite3.connect(plan["database"])) as connection, connection:
        assert connection.execute("SELECT word FROM memory").fetchone()[0] == "AMBER"


def test_backup_includes_committed_wal(plan, tmp_path):
    connection = sqlite3.connect(plan["database"])
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE memory SET word='WAL_WORD'")
        connection.commit()
        worker.snapshot(plan, tmp_path / "backup")
        with closing(sqlite3.connect(tmp_path / "backup/database")) as backup, backup:
            assert backup.execute("SELECT word FROM memory").fetchone()[0] == "WAL_WORD"
    finally:
        connection.close()


def test_success_keeps_recovery_snapshot(plan, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "health", lambda plan: None)
    assert worker.execute(plan, tmp_path / "backup") == 0
    assert json.loads((tmp_path / "backup/plan.json").read_text())["operation"] == "update"


def test_rollback_swaps_recovery_history(plan, tmp_path, monkeypatch):
    history = tmp_path / "history"
    history.mkdir()
    plan["current"] = str(history / "current.json")
    previous = history / "previous"
    worker.snapshot(plan, previous)
    Path(plan["current"]).write_text(json.dumps({"backup": str(previous)}))
    (Path(plan["prefix"]) / "version").write_text("new")
    Path(plan["entry"]).write_text("new-entry")
    with closing(sqlite3.connect(plan["database"])) as connection, connection:
        connection.execute("UPDATE memory SET word='NEW'")
    plan["operation"] = "rollback"
    monkeypatch.setattr(worker, "health", lambda plan: None)
    recovery = history / "recovery"
    assert worker.execute(plan, recovery) == 0
    assert (Path(plan["prefix"]) / "version").read_text() == "old"
    assert Path(plan["entry"]).read_text() == "old-entry"
    with closing(sqlite3.connect(plan["database"])) as connection, connection:
        assert connection.execute("SELECT word FROM memory").fetchone()[0] == "AMBER"
    assert (recovery / "environment/version").read_text() == "new"
    assert json.loads(Path(plan["current"]).read_text())["backup"] == str(recovery)
    assert not previous.exists()


def test_rollback_refuses_different_database(plan, tmp_path):
    plan["current"] = str(tmp_path / "current.json")
    previous = tmp_path / "previous"
    worker.snapshot(plan, previous)
    Path(plan["current"]).write_text(json.dumps({"backup": str(previous)}))
    plan["operation"] = "rollback"
    plan["database"] = str(tmp_path / "another.db")
    with pytest.raises(ValueError, match="another installation/configuration"):
        worker.execute(plan, tmp_path / "backup")
    assert previous.is_dir()
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("outcome", [0, 1, "exception"])
def test_worker_persists_actual_outcome_for_async_callers(plan, tmp_path, monkeypatch, outcome):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    manifest = handoff / "plan.json"
    plan["backup"] = str(tmp_path / "backup")
    plan["result"] = str(tmp_path / "result.json")
    manifest.write_text(json.dumps(plan))
    monkeypatch.setattr(worker.sys, "argv", ["worker.py", str(manifest)])

    def execute(*args):
        if outcome == "exception":
            raise ValueError("failed before package replacement")
        return outcome

    monkeypatch.setattr(worker, "execute", execute)
    expected = 0 if outcome == 0 else 1
    assert worker.main() == expected
    result = json.loads(Path(plan["result"]).read_text())
    assert result["returncode"] == expected
    assert result["status"] == ("succeeded" if expected == 0 else "failed")
    assert not handoff.exists()


def test_crashed_installer_subprocess_restores_wal_and_preserves_private_files(plan, tmp_path):
    import sys

    private = {}
    for name in ("config.yaml", ".env", "connector.key"):
        file = tmp_path / name
        file.write_bytes(os.urandom(64))
        private[file] = file.read_bytes()
    code = (
        "import pathlib, sqlite3, sys, os; "
        "pathlib.Path(sys.argv[1]).write_text('partial'); "
        "pathlib.Path(sys.argv[2]).write_text('partial-entry'); "
        "c=sqlite3.connect(sys.argv[3]); c.execute('PRAGMA journal_mode=WAL'); "
        "c.execute(\"UPDATE memory SET word='CRASHED'\"); "
        "c.execute('PRAGMA user_version=999'); c.commit(); os._exit(37)"
    )
    plan["command"] = [
        sys.executable,
        "-c",
        code,
        str(Path(plan["prefix"]) / "version"),
        plan["entry"],
        plan["database"],
    ]
    assert worker.execute(plan, tmp_path / "crash-backup") == 1
    assert (Path(plan["prefix"]) / "version").read_text() == "old"
    assert Path(plan["entry"]).read_text() == "old-entry"
    with closing(sqlite3.connect(plan["database"])) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT word FROM memory").fetchone()[0] == "AMBER"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert all(file.read_bytes() == original for file, original in private.items())


def test_failed_first_install_health_removes_new_database(plan, tmp_path, monkeypatch):
    Path(plan["database"]).unlink()
    monkeypatch.setattr(worker, "run", lambda *args, **kwargs: None)

    def migrate_then_fail(plan):
        with closing(sqlite3.connect(plan["database"])) as connection, connection:
            connection.execute("CREATE TABLE new_schema (id INTEGER)")
        raise ValueError("new release health failed")

    monkeypatch.setattr(worker, "health", migrate_then_fail)
    assert worker.execute(plan, tmp_path / "backup") == 1
    assert not Path(plan["database"]).exists()
    assert (Path(plan["prefix"]) / "version").read_text() == "old"


def test_failed_restore_keeps_recovery_snapshot(plan, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr(worker, "run", fail)
    monkeypatch.setattr(worker, "restore", fail)
    with pytest.raises(OSError):
        worker.execute(plan, tmp_path / "backup")
    assert (tmp_path / "backup/environment/version").read_text() == "old"
    assert (tmp_path / "backup/database").is_file()
    assert (tmp_path / "backup/plan.json").is_file()


def test_recovery_snapshot_preserves_delivery_cursor_and_selection(plan, tmp_path):
    from acp_gateway.storage import Conversation, JobStatus, Store

    store = Store.open(plan["database"])
    session = store.add_session(Conversation("telegram", "12345", "work"), "s", "/work")
    store.add_job("delivered-prefix", session.id, delivery_namespace="telegram:123456")
    store.finish_job("delivered-prefix", JobStatus.COMPLETED, answer="saved answer")
    store.deliveries.claim("telegram:123456", "delivered-prefix")
    store.deliveries.acknowledge("telegram:123456", "delivered-prefix", last=False)
    store.close()
    worker.snapshot(plan, tmp_path / "backup")
    store = Store.open(plan["database"])
    store.deliveries.claim("telegram:123456", "delivered-prefix")
    store.deliveries.acknowledge("telegram:123456", "delivered-prefix", last=True)
    store.close()
    worker.restore(plan, tmp_path / "backup")
    store = Store.open(plan["database"])
    try:
        row = store.deliveries.get("telegram:123456", "delivered-prefix")
        assert row["state"] == "unknown" and row["next_chunk"] == 1
        assert store.deliveries.due("telegram:123456") is None
        assert store.active_session(Conversation("telegram", "12345", "work")).id == session.id
        assert store.job("delivered-prefix").answer == "saved answer"
    finally:
        store.close()
