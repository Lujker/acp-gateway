"""Credential lifecycle and atomic owner administration."""

import json
import sqlite3
from importlib import resources

import pytest

from acp_gateway.cli.main import main
from acp_gateway.storage.db import Store


@pytest.fixture
def store(tmp_path):
    db = Store.open_in(tmp_path / "data")
    yield db
    db.close()


def test_lifecycle_and_identity_binding(store, tmp_path):
    registry = store.computers
    first, second = tmp_path / "first.key", tmp_path / "second.key"
    registry.issue("work", first, display_name="Work laptop")
    token = first.read_text().strip()
    assert first.stat().st_mode & 0o077 == 0
    assert registry.authenticate("work", token)
    assert not registry.authenticate("other", token)
    assert not registry.authenticate("work", "incorrect")
    assert token not in json.dumps(registry.list())
    assert "credential_digest" not in registry.list()[0]
    registry.issue("work", second)
    new_token = second.read_text().strip()
    assert not registry.authenticate("work", token)
    assert registry.authenticate("work", new_token)
    assert registry.list()[0]["generation"] == 2
    registry.revoke("work")
    registry.revoke("work")
    assert not registry.authenticate("work", new_token)
    with pytest.raises(ValueError, match="revoked"):
        registry.issue("work", tmp_path / "third.key")
    with pytest.raises(ValueError, match="already enrolled"):
        registry.issue("work", tmp_path / "fourth.key", display_name="Re-enroll")
    # Raw credentials must not appear in the SQLite database or WAL.
    for file in (tmp_path / "data").iterdir():
        assert token.encode() not in file.read_bytes()
        assert new_token.encode() not in file.read_bytes()


def test_existing_file_and_symlink_never_overwritten(store, tmp_path):
    path = tmp_path / "existing"
    path.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(path)
    for destination in (path, link):
        with pytest.raises(FileExistsError):
            store.computers.issue("work", destination, display_name="Work")
        assert store.computers.list() == []
    assert path.read_text() == "keep"
    assert link.is_symlink()


def test_write_failure_preserves_old_credential(store, tmp_path, monkeypatch):
    import acp_gateway.storage.computers as module

    store.computers.issue("work", tmp_path / "old", display_name="Work")
    old = (tmp_path / "old").read_text().strip()

    def fail(fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError):
        store.computers.issue("work", tmp_path / "new")
    assert not (tmp_path / "new").exists()
    assert store.computers.authenticate("work", old)
    assert store.computers.list()[0]["generation"] == 1


def test_database_failure_removes_export_and_rolls_back(store, tmp_path):
    store._conn.execute(
        "CREATE TRIGGER fail_enroll BEFORE INSERT ON computers "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.Error):
        store.computers.issue("work", tmp_path / "key", display_name="Work")
    assert not (tmp_path / "key").exists()
    assert store.computers.list() == []


@pytest.mark.parametrize("computer_id", ["../work", "Work", "", "a" * 65, "a\n"])
def test_invalid_identity(store, tmp_path, computer_id):
    with pytest.raises(ValueError, match="invalid computer ID"):
        store.computers.issue(computer_id, tmp_path / "key", display_name="Work")
    assert not (tmp_path / "key").exists()


def test_upgrade_preserves_channel_cursor(tmp_path):
    path = tmp_path / "gateway.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrations = resources.files("acp_gateway.storage.migrations")
    for name in ("0001_initial.sql", "0002_approvals.sql", "0003_channel_state.sql"):
        # Find the numbered migration independent of its descriptive suffix.
        entry = next(p for p in migrations.iterdir() if p.name.startswith(name[:4]))
        conn.executescript(entry.read_text())
    conn.execute("PRAGMA user_version = 3")
    old = Store(conn)
    old.set_channel_state("telegram", "cursor", {"id": 77})
    old.close()
    upgraded = Store.open(path)
    try:
        assert upgraded.schema_version == 5
        assert upgraded.channel_state("telegram", "cursor") == {"id": 77}
    finally:
        upgraded.close()


def test_cli_local_lifecycle_without_api(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path / 'data'}\n")
    env_file = tmp_path / "test.env"
    env_file.write_text("")
    prefix = ["--config", str(config), "--env-file", str(env_file), "computers"]
    first, second = tmp_path / "one.key", tmp_path / "two.key"
    assert main([*prefix, "enroll", "work", "--name", "Work", "--token-file", str(first)]) == 0
    token = first.read_text().strip()
    assert token not in capsys.readouterr().out
    assert main([*prefix, "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["computer_id"] == "work"
    assert main([*prefix, "rotate", "work", "--token-file", str(second)]) == 0
    assert main([*prefix, "revoke", "work"]) == 0
    assert main([*prefix, "rotate", "work", "--token-file", str(tmp_path / "third")]) == 1
    assert "revoked" in capsys.readouterr().err


def test_changes_visible_across_owner_process_connections(store, tmp_path):
    other = Store.open_in(tmp_path / "data")
    try:
        store.computers.issue("work", tmp_path / "first", display_name="Work")
        first = (tmp_path / "first").read_text().strip()
        with pytest.raises(ValueError, match="already enrolled"):
            other.computers.issue("work", tmp_path / "duplicate", display_name="Work")
        assert not (tmp_path / "duplicate").exists()
        other.computers.issue("work", tmp_path / "rotated")
        second = (tmp_path / "rotated").read_text().strip()
        assert not store.computers.authenticate("work", first)
        assert store.computers.authenticate("work", second)
        other.computers.revoke("work")
        assert not store.computers.authenticate("work", second)
    finally:
        other.close()


def test_cli_database_open_error_is_safe(tmp_path, monkeypatch, capsys):
    config, env_file = tmp_path / "config.yaml", tmp_path / "test.env"
    config.write_text(f"data_dir: {tmp_path / 'data'}\n")
    env_file.write_text("")

    def fail(cls, data_dir):
        raise sqlite3.OperationalError("sensitive database detail")

    monkeypatch.setattr(Store, "open_in", classmethod(fail))
    assert main(["--config", str(config), "--env-file", str(env_file), "computers", "list"]) == 1
    error = capsys.readouterr().err
    assert "registry operation failed" in error
    assert "sensitive" not in error
