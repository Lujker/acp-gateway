import json
import sqlite3
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
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE memory (word TEXT)")
        connection.execute("INSERT INTO memory VALUES ('AMBER')")
    return {
        "operation": "update",
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
        with sqlite3.connect(plan["database"]) as connection:
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
    with sqlite3.connect(plan["database"]) as connection:
        assert connection.execute("SELECT word FROM memory").fetchone()[0] == "AMBER"


def test_backup_includes_committed_wal(plan, tmp_path):
    connection = sqlite3.connect(plan["database"])
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE memory SET word='WAL_WORD'")
        connection.commit()
        worker.snapshot(plan, tmp_path / "backup")
        with sqlite3.connect(tmp_path / "backup/database") as backup:
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
    with sqlite3.connect(plan["database"]) as connection:
        connection.execute("UPDATE memory SET word='NEW'")
    plan["operation"] = "rollback"
    monkeypatch.setattr(worker, "health", lambda plan: None)
    recovery = history / "recovery"
    assert worker.execute(plan, recovery) == 0
    assert (Path(plan["prefix"]) / "version").read_text() == "old"
    assert Path(plan["entry"]).read_text() == "old-entry"
    with sqlite3.connect(plan["database"]) as connection:
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


def test_windows_staging_keeps_waiting_callers_files_separate(plan, tmp_path):
    backup = tmp_path / "backup"
    worker.snapshot(plan, backup)
    worker.park_windows(plan, backup)
    (Path(plan["prefix"]) / "version").write_text("new")
    Path(plan["entry"]).write_text("new-entry")
    parked_prefix, parked_entry = map(Path, plan["parked"])
    assert (parked_prefix / "version").read_text() == "old"
    assert parked_entry.read_text() == "old-entry"
    worker.restore(plan, backup)
    assert (Path(plan["prefix"]) / "version").read_text() == "old"
    assert Path(plan["entry"]).read_text() == "old-entry"
