"""SQLite storage: sessions, conversations, jobs and approval audit.

The standard ``sqlite3`` module is used synchronously from the event loop:
every statement touches a handful of rows of a local file, so a worker thread
(``aiosqlite``) would add more overhead than it removes. Migrations are the
numbered ``migrations/NNNN_*.sql`` scripts, tracked by ``PRAGMA user_version``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime
from functools import cached_property
from importlib import resources
from pathlib import Path
from typing import Any

from acp_gateway.log import redact_text, redact_value
from acp_gateway.storage.records import (
    ApprovalAudit,
    Conversation,
    Job,
    JobStatus,
    SessionRecord,
    utcnow,
)

DB_FILENAME = "gateway.db"
_BUSY_TIMEOUT_MS = 5000
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_SESSION_COLUMNS = (
    "s.id, s.channel, s.conversation_key, s.agent, s.acp_session_id, s.cwd, s.title, "
    "s.created_at, s.last_used_at"
)
_JOB_COLUMNS = (
    "j.id, j.session_id, j.status, j.answer, j.stop_reason, j.error, j.usage, "
    "j.created_at, j.finished_at, s.channel, s.conversation_key, s.agent, s.acp_session_id"
)


def _migrations() -> list[tuple[int, str]]:
    found = []
    for entry in resources.files("acp_gateway.storage.migrations").iterdir():
        if match := _MIGRATION_NAME.match(entry.name):
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


def _statements(script: str) -> Iterator[str]:
    """Split a migration script into statements; ``;`` inside literals or triggers stays put."""
    statement = ""
    parts = script.split(";")
    for index, part in enumerate(parts):
        statement += part if index == len(parts) - 1 else part + ";"
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        yield statement


def _enable_wal(conn: sqlite3.Connection) -> None:
    # Switching a fresh file to WAL can report "database is locked" without
    # consulting busy_timeout while another process does the same; retry within it.
    deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


class SchemaTooNewError(ValueError):
    """The database was migrated by a newer acpgw (for example before a binary rollback)."""


def _ts(value: datetime) -> str:
    return value.isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: Path | str) -> Store:
        """Open (creating if needed) and migrate the database; ``":memory:"`` is allowed."""
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.touch(mode=0o600, exist_ok=True)
            if os.name == "posix":
                path.chmod(0o600)
                # Existing WAL files can contain answers too; SQLite preserves
                # their permissions when reopening an existing database.
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{path}{suffix}")
                    if sidecar.is_file():
                        sidecar.chmod(0o600)
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            if str(path) != ":memory:":
                _enable_wal(conn)
            store = cls(conn)
            store.migrate()
        except BaseException:
            conn.close()
            raise
        return store

    @classmethod
    def open_in(cls, data_dir: Path) -> Store:
        return cls.open(data_dir / DB_FILENAME)

    @cached_property
    def computers(self):
        from acp_gateway.storage.computers import ComputerRegistry

        return ComputerRegistry(self._conn)

    def close(self) -> None:
        self._conn.close()

    def channel_state(self, namespace: str, key: str, default=None):
        row = self._conn.execute(
            "SELECT value FROM channel_state WHERE namespace = ? AND key = ?", (namespace, key)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set_channel_state(self, namespace: str, key: str, value) -> None:
        self._conn.execute(
            "INSERT INTO channel_state (namespace, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
            (namespace, key, json.dumps(value)),
        )

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        """Apply pending migrations atomically; safe against concurrent openers.

        The write lock is taken (``BEGIN IMMEDIATE``) before ``user_version`` is
        re-read, so a second process waits and then sees the migrated schema.
        Statements run one by one because ``executescript()`` would commit the
        open transaction first.
        """
        migrations = _migrations()
        latest = migrations[-1][0] if migrations else 0
        if self._check_version(latest) == latest:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._check_version(latest)
            for version, script in migrations:
                if version <= current:
                    continue
                for statement in _statements(script):
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version = {version:d}")
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _check_version(self, latest: int) -> int:
        current = self.schema_version
        if current > latest:
            raise SchemaTooNewError(
                f"database schema {current} is newer than this acpgw supports ({latest}); "
                "upgrade acpgw"
            )
        return current

    # ---------------------------------------------------------------- sessions

    def add_session(
        self, conversation: Conversation, acp_session_id: str, cwd: str, *, activate: bool = True
    ) -> SessionRecord:
        now = _ts(utcnow())
        with self._transaction():
            cursor = self._conn.execute(
                "INSERT INTO sessions (channel, conversation_key, agent, acp_session_id, cwd,"
                " created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation.channel,
                    conversation.key,
                    conversation.agent,
                    acp_session_id,
                    cwd,
                    now,
                    now,
                ),
            )
            session_id = cursor.lastrowid
            if activate:
                self._set_active(conversation, session_id)
        return self._require(self.session(session_id))

    def session(self, session_id: int) -> SessionRecord | None:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s WHERE s.id = ?",  # noqa: S608
            (session_id,),
        ).fetchone()
        return self._session(row) if row else None

    def find_session(self, conversation: Conversation, acp_session_id: str) -> SessionRecord | None:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s"  # noqa: S608
            " WHERE s.channel = ? AND s.conversation_key = ? AND s.agent = ?"
            " AND s.acp_session_id = ?",
            (*self._key(conversation), acp_session_id),
        ).fetchone()
        return self._session(row) if row else None

    def sessions(self, conversation: Conversation) -> list[SessionRecord]:
        """Sessions of a conversation, most recently used first."""
        rows = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s"  # noqa: S608
            " WHERE s.channel = ? AND s.conversation_key = ? AND s.agent = ?"
            " ORDER BY s.last_used_at DESC, s.id DESC",
            self._key(conversation),
        ).fetchall()
        return [self._session(row) for row in rows]

    def active_session(self, conversation: Conversation) -> SessionRecord | None:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM conversations c"  # noqa: S608
            " JOIN sessions s ON s.id = c.active_session_id"
            " WHERE c.channel = ? AND c.conversation_key = ? AND c.agent = ?",
            self._key(conversation),
        ).fetchone()
        return self._session(row) if row else None

    def set_active(self, conversation: Conversation, session_id: int) -> None:
        with self._transaction():
            self._set_active(conversation, session_id)

    def touch_session(self, session_id: int) -> None:
        self._conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE id = ?", (_ts(utcnow()), session_id)
        )

    def set_session_title(self, session_id: int, title: str) -> None:
        self._conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    # -------------------------------------------------------------------- jobs

    def add_job(self, job_id: str, session_id: int) -> Job:
        self._conn.execute(
            "INSERT INTO jobs (id, session_id, status, created_at) VALUES (?, ?, ?, ?)",
            (job_id, session_id, JobStatus.RUNNING.value, _ts(utcnow())),
        )
        return self._require(self.job(job_id))

    def finish_job(
        self,
        job_id: str,
        status: JobStatus,
        *,
        answer: str = "",
        stop_reason: str | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> Job:
        self._conn.execute(
            "UPDATE jobs SET status = ?, answer = ?, stop_reason = ?, error = ?, usage = ?,"
            " finished_at = ? WHERE id = ?",
            (
                status.value,
                answer,
                stop_reason,
                error,
                json.dumps(usage) if usage is not None else None,
                _ts(utcnow()),
                job_id,
            ),
        )
        return self._require(self.job(job_id))

    def job(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs j"  # noqa: S608
            " JOIN sessions s ON s.id = j.session_id WHERE j.id = ?",
            (job_id,),
        ).fetchone()
        return self._job(row) if row else None

    def interrupt_running_jobs(self) -> int:
        """Mark jobs left running by a previous process as interrupted."""
        cursor = self._conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE status = ?",
            (
                JobStatus.INTERRUPTED.value,
                "the gateway restarted while the job was running",
                _ts(utcnow()),
                JobStatus.RUNNING.value,
            ),
        )
        return cursor.rowcount

    def prune_jobs(self, finished_before: datetime) -> int:
        cursor = self._conn.execute(
            "DELETE FROM jobs WHERE status != ? AND finished_at < ?",
            (JobStatus.RUNNING.value, _ts(finished_before)),
        )
        return cursor.rowcount

    # --------------------------------------------------------------- approvals

    def record_approval(self, audit: ApprovalAudit) -> None:
        """Persist a decision before replying to the agent; never persist raw secrets."""
        self._conn.execute(
            "INSERT INTO approvals_audit (id, job_id, channel, conversation_key, agent,"
            " acp_session_id, tool_call_id, title, kind, raw_input, requested_at, resolved_at,"
            " outcome, option_id, decided_channel, actor, reason) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit.id,
                audit.job_id,
                *self._key(audit.conversation),
                audit.acp_session_id,
                audit.tool_call_id,
                redact_text(audit.title) if audit.title else None,
                audit.kind,
                json.dumps(redact_value(audit.raw_input)),
                _ts(audit.requested_at),
                _ts(audit.resolved_at),
                audit.outcome,
                audit.option_id,
                audit.decided_channel,
                redact_text(audit.actor) if audit.actor else None,
                redact_text(audit.reason) if audit.reason else None,
            ),
        )

    def approval_audit(self, job_id: str | None = None) -> list[ApprovalAudit]:
        query = "SELECT * FROM approvals_audit"
        params = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            params = (job_id,)
        rows = self._conn.execute(query + " ORDER BY resolved_at, rowid", params).fetchall()
        return [
            ApprovalAudit(
                id=r["id"],
                job_id=r["job_id"],
                conversation=Conversation(r["channel"], r["conversation_key"], r["agent"]),
                acp_session_id=r["acp_session_id"],
                tool_call_id=r["tool_call_id"],
                title=r["title"],
                kind=r["kind"],
                raw_input=json.loads(r["raw_input"]),
                requested_at=datetime.fromisoformat(r["requested_at"]),
                resolved_at=datetime.fromisoformat(r["resolved_at"]),
                outcome=r["outcome"],
                option_id=r["option_id"],
                decided_channel=r["decided_channel"],
                actor=r["actor"],
                reason=r["reason"],
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- helpers

    def _transaction(self) -> sqlite3.Connection:
        # In autocommit mode a connection context manager does not open a
        # transaction, so open one explicitly; the context manager commits it.
        self._conn.execute("BEGIN")
        return self._conn

    def _set_active(self, conversation: Conversation, session_id: int | None) -> None:
        self._conn.execute(
            "INSERT INTO conversations (channel, conversation_key, agent, active_session_id)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (channel, conversation_key, agent)"
            " DO UPDATE SET active_session_id = excluded.active_session_id",
            (*self._key(conversation), session_id),
        )

    @staticmethod
    def _require[R](record: R | None) -> R:
        if record is None:
            raise LookupError("a row written in this call is missing")
        return record

    @staticmethod
    def _key(conversation: Conversation) -> tuple[str, str, str]:
        return (conversation.channel, conversation.key, conversation.agent)

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            conversation=Conversation(row["channel"], row["conversation_key"], row["agent"]),
            acp_session_id=row["acp_session_id"],
            cwd=row["cwd"],
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used_at=datetime.fromisoformat(row["last_used_at"]),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            session_id=row["session_id"],
            conversation=Conversation(row["channel"], row["conversation_key"], row["agent"]),
            acp_session_id=row["acp_session_id"],
            status=JobStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            answer=row["answer"],
            stop_reason=row["stop_reason"],
            error=row["error"],
            usage=json.loads(row["usage"]) if row["usage"] else None,
            finished_at=_dt(row["finished_at"]),
        )
