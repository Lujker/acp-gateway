"""Stdlib durable update intents, shared with the independent updater process."""

import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import UUID, uuid4

ACTIVE = ("awaiting_confirmation", "dispatching", "queued", "running", "unknown")
TERMINAL = ("succeeded", "failed", "cancelled")


def operation_id(value):
    return UUID(str(value)).hex


class Journal:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("Update journal must not be a symlink")
        path.touch(mode=0o600, exist_ok=True)
        if os.name == "posix":
            path.chmod(0o600)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL,
                role TEXT NOT NULL, target TEXT NOT NULL,
                version TEXT NOT NULL, current_version TEXT NOT NULL,
                binding TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_target ON operations(role, target)
                WHERE status IN
                    ('awaiting_confirmation','dispatching','queued','running','unknown');
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self):
        self.conn.close()

    def get(self, key, *, actor=None):
        row = self.conn.execute(
            "SELECT * FROM operations WHERE id=?", (operation_id(key),)
        ).fetchone()
        if row is None or (actor is not None and row["actor"] != actor):
            raise ValueError("Update operation is unavailable for this actor")
        return dict(row)

    def create(
        self,
        *,
        actor,
        role,
        target,
        version,
        current_version,
        binding,
        expires,
        key=None,
        status="awaiting_confirmation",
    ):
        now = time.time()
        key = operation_id(key) if key else uuid4().hex
        self.conn.execute(
            "UPDATE operations SET status='cancelled',reason='expired',updated=? "
            "WHERE status='awaiting_confirmation' AND expires<?",
            (now, now),
        )
        self.conn.execute(
            "INSERT INTO operations "
            "(id,actor,role,target,version,current_version,binding,status,created,expires,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                actor,
                role,
                target,
                version,
                current_version,
                binding,
                status,
                now,
                expires,
                now,
            ),
        )
        return self.get(key)

    def transition(self, key, expected, status, reason=""):
        values = (status, reason, time.time(), operation_id(key), *expected)
        placeholders = ",".join("?" for _ in expected)
        return (
            self.conn.execute(
                f"UPDATE operations SET status=?,reason=?,updated=? WHERE id=? "  # noqa: S608 — ? placeholders
                f"AND status IN ({placeholders})",
                values,
            ).rowcount
            == 1
        )

    def recent(self, *, actor=None, limit=10):
        where, values = ("WHERE actor=?", (actor,)) if actor is not None else ("", ())
        return [
            dict(row)
            for row in self.conn.execute(
                f"SELECT * FROM operations {where} ORDER BY created DESC LIMIT ?",  # noqa: S608 — fixed clauses
                (*values, limit),
            )
        ]

    def unfinished(self):
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM operations WHERE status IN ('queued','running','unknown') "
                "ORDER BY created"
            )
        ]

    def set_worker(self, value):
        self.conn.execute(
            "INSERT INTO metadata(key,value) VALUES ('worker',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(value),),
        )

    def resolution(self, key):
        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key=?", ("resolution:" + operation_id(key),)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def resolve(self, key, outcome, audit):
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("Invalid local resolution")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self.transition(
                key, ("unknown",), outcome, "" if outcome == "succeeded" else "failed"
            ):
                raise ValueError("Operation changed during local resolution")
            self.conn.execute(
                "INSERT INTO metadata(key,value) VALUES (?,?)",
                ("resolution:" + operation_id(key), json.dumps(audit)),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def worker(self):
        row = self.conn.execute("SELECT value FROM metadata WHERE key='worker'").fetchone()
        return json.loads(row[0]) if row else None

    def notification_candidates(self, actor):
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT o.* FROM operations o WHERE actor=? AND "
                "(status IN ('dispatching','queued','running','unknown') OR "
                "(status IN ('succeeded','failed') AND NOT EXISTS "
                "(SELECT 1 FROM metadata m WHERE m.key='notice:'||o.id||':'||o.status))) "
                "ORDER BY updated LIMIT 10",
                (actor,),
            )
        ]

    def notice(self, key, status, value=None):
        name = "notice:" + operation_id(key) + ":" + status
        if value is not None:
            self.conn.execute(
                "INSERT INTO metadata(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (name, json.dumps(value)),
            )
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None
