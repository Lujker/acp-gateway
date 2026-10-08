"""Migration preserves enrollment; stored epochs never imply live connectivity."""

import sqlite3
from importlib import resources
from uuid import uuid4

from acp_gateway.connectors.control import ControlDispatcher
from acp_gateway.storage import Store


def test_upgrade_from_schema4_preserves_enrollment_and_new_diagnostics(tmp_path):
    path = tmp_path / "gateway.db"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    migrations = resources.files("acp_gateway.storage.migrations")
    for version in range(1, 5):
        entry = next(p for p in migrations.iterdir() if p.name.startswith(f"{version:04}_"))
        connection.executescript(entry.read_text())
    connection.execute("PRAGMA user_version = 4")
    old = Store(connection)
    key = tmp_path / "computer.key"
    old.computers.issue("work", key, display_name="Work")
    token = key.read_text().strip()
    old.close()
    upgraded = Store.open(path)
    try:
        assert upgraded.schema_version == 5
        assert upgraded.computers.authenticate("work", token)
        status = upgraded.computers.connection_status()[0]
        assert status["connected_at"] is None and status["agents"] == []
        assert "credential_digest" not in status
    finally:
        upgraded.close()


def test_retired_epoch_cannot_overwrite_current_diagnostics(tmp_path):
    store = Store.open(":memory:")
    try:
        registry = store.computers
        registry.issue("work", tmp_path / "key", display_name="Work")
        old, current = uuid4(), uuid4()
        registry.connection_opened("work", old, ["goose"])
        registry.connection_opened("work", current, ["goose"])
        registry.connection_closed("work", old, "replaced", 1008)
        assert registry.connection_status()[0]["disconnected_at"] is None
        # Simulate abrupt termination: the persisted open row is settled on restart.
        dispatcher = ControlDispatcher(registry)
        status = dispatcher.status()[0]
        assert status["epoch"] == str(current)
        assert status["disconnect_reason"] == "listener_restarted"
        assert status["disconnected_at"] and not status["connected"]
    finally:
        store.close()
