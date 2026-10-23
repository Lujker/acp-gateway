"""Cross-milestone wiring: out-of-box updater startup and shared artifact gates."""

import asyncio
import os
import sqlite3
from contextlib import closing

import pytest
from pydantic import SecretStr

from acp_gateway import update_bootstrap
from acp_gateway.api.app import create_app
from acp_gateway.channels.cli import CliChannel
from acp_gateway.config import UpdateSettings
from acp_gateway.core import GatewayCore
from acp_gateway.maintenance_worker import restore, snapshot
from acp_gateway.storage import Store


def test_updates_are_enabled_out_of_the_box_but_opt_out_is_preserved():
    assert UpdateSettings().enabled is True
    assert UpdateSettings(enabled=False).enabled is False


def test_background_startup_runs_after_lifespan_can_serve_and_is_cancelled(tmp_path):
    store = Store.open_in(tmp_path)
    core = GatewayCore(store, {})
    cli = CliChannel()
    core.add_channel(cli)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def startup():
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()

    app = create_app(core, cli, SecretStr("owner-integration-credential"), startup=startup)

    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), 2)

    asyncio.run(run())
    assert cancelled.is_set()


def test_updater_stage_runtime_action_does_not_load_configuration(monkeypatch, tmp_path):
    from acp_gateway.cli import main as cli_main

    called = []

    def stage(path):
        called.append(path)

    monkeypatch.setattr(update_bootstrap, "stage_runtime", stage)

    def unexpected(*args, **kwargs):
        raise AssertionError("stage-runtime must not load configuration or services")

    monkeypatch.setattr(cli_main, "_run_configured", unexpected)
    policy = tmp_path / "updater.json"
    assert cli_main.main(["updater", "--policy", str(policy), "stage-runtime"]) == 0
    assert called == [policy]


"""The pairing boundary survives an application database rollback."""


def test_database_rollback_does_not_delete_pairing_boundary(tmp_path):
    prefix = tmp_path / "environment"
    prefix.mkdir()
    (prefix / "version.txt").write_text("old")
    entry = tmp_path / "acpgw"
    entry.write_text("old entry")
    data = tmp_path / "data"
    data.mkdir()
    database = data / "gateway.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('current')")
    boundary = data / "pairing-boundary.json"
    boundary.write_text('{"boundary": "activated"}')
    backup = tmp_path / "backup"
    plan = {
        "prefix": str(prefix),
        "entry": str(entry),
        "database": str(database),
    }

    snapshot(plan, backup)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE marker SET value='new release'")
        connection.commit()
    assert not (backup / "pairing-boundary.json").exists()

    restore(plan, backup)

    assert boundary.read_text() == '{"boundary": "activated"}'
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("current",)


@pytest.fixture
def rollback_case(tmp_path):
    def build(journal):
        prefix = tmp_path / "environment"
        prefix.mkdir()
        (prefix / "version.txt").write_text("old")
        entry = tmp_path / "acpgw"
        entry.write_text("old entry")
        database = tmp_path / "gateway.db"
        with closing(sqlite3.connect(database)) as connection, connection:
            assert (
                connection.execute("PRAGMA journal_mode=" + journal).fetchone()[0]
                == journal.lower()
            )
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('current')")
            connection.execute("CREATE TABLE outbound_deliveries (state TEXT)")
            connection.executemany(
                "INSERT INTO outbound_deliveries VALUES (?)",
                [(state,) for state in ("waiting", "pending", "sending", "sent")],
            )
        boundary = tmp_path / "pairing-boundary.json"
        boundary.write_text("unchanged boundary")
        plan = {"prefix": str(prefix), "entry": str(entry), "database": str(database)}
        backup = tmp_path / "backup"
        snapshot(plan, backup)
        (prefix / "version.txt").write_text("new release")
        entry.write_text("new entry")
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE marker SET value='new release'")
        return plan, backup, prefix, entry, database, boundary

    return build


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_closed_handle_restore_preserves_boundary_and_delivery_safety(rollback_case, journal):
    plan, backup, prefix, entry, database, boundary = rollback_case(journal)
    restore(plan, backup)
    assert (prefix / "version.txt").read_text() == "old"
    assert entry.read_text() == "old entry"
    assert boundary.read_text() == "unchanged boundary"
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("current",)
        assert connection.execute("SELECT state FROM outbound_deliveries").fetchall() == [
            ("unknown",),
            ("unknown",),
            ("unknown",),
            ("sent",),
        ]


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_external_open_handle_documents_partial_restore_risk(rollback_case, journal):
    """Risk characterization, not an atomic-restore guarantee or runtime repair."""
    plan, backup, prefix, entry, database, boundary = rollback_case(journal)
    with closing(sqlite3.connect(database)) as external, external:
        assert external.execute("SELECT value FROM marker").fetchone() == ("new release",)
        if os.name == "nt":
            with pytest.raises(PermissionError) as error:
                restore(plan, backup)
            assert error.value.winerror in {5, 32}
            # Environment and entry have already rolled back but the DB has not.
            with closing(sqlite3.connect(database)) as current, current:
                assert current.execute("SELECT value FROM marker").fetchone() == ("new release",)
        else:
            restore(plan, backup)
            # POSIX replacement leaves the external reader on its old inode.
            assert external.execute("SELECT value FROM marker").fetchone() == ("new release",)
        # restore never owns or closes this external connection.
        assert external.execute("SELECT 1").fetchone() == (1,)
        assert (prefix / "version.txt").read_text() == "old"
        assert entry.read_text() == "old entry"
        assert boundary.read_text() == "unchanged boundary"
