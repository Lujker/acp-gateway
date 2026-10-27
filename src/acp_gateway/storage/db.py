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
    JobSummary,
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

    @cached_property
    def models(self):
        from acp_gateway.storage.models import ModelStateStore

        return ModelStateStore(self._conn)

    @cached_property
    def deliveries(self):
        from acp_gateway.storage.deliveries import DeliveryStore

        return DeliveryStore(self._conn)

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

    def set_channel_states(self, namespace: str, values: dict) -> None:
        """Commit related durable UI/control records together, before a remote side effect."""
        with self._transaction():
            for key, value in values.items():
                self.set_channel_state(namespace, key, value)

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
        self,
        conversation: Conversation,
        acp_session_id: str,
        cwd: str,
        *,
        activate: bool = True,
        binding=None,
        expected_active_revision: int | None = None,
        model_default_required: dict | None = None,
    ) -> SessionRecord:
        now = _ts(utcnow())
        from acp_gateway.agents.bindings import session_binding

        binding = session_binding(binding)
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
            if model_default_required is not None:
                self.set_channel_state(
                    "model_default_required", str(session_id), model_default_required
                )
            if binding is not None:
                self.set_channel_state("agent_session_binding", str(session_id), binding)
            if activate and (
                expected_active_revision is None
                or self.active_revision(conversation) == expected_active_revision
            ):
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

    def recent_sessions(self, channel: str, key: str, limit: int) -> list[SessionRecord]:
        rows = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s"  # noqa: S608
            " WHERE s.channel = ? AND s.conversation_key = ?"
            " ORDER BY s.last_used_at DESC, s.id DESC LIMIT ?",
            (channel, key, limit),
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

    def active_revision(self, conversation: Conversation) -> int:
        """Durable selection generation; explicit re-selection counts as intent."""
        if self.schema_version < 3:
            return 0  # Legacy schema construction used by migration tooling.
        key = json.dumps(self._key(conversation), separators=(",", ":"))
        value = self.channel_state("active_selection_revision", key, 0)
        if type(value) is not int or value < 0:
            raise ValueError("invalid active dialogue selection revision")
        return value

    def touch_session(self, session_id: int) -> None:
        self._conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE id = ?", (_ts(utcnow()), session_id)
        )

    def set_session_title(self, session_id: int, title: str) -> None:
        self._conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    # -------------------------------------------------------------------- jobs

    def add_job(
        self,
        job_id: str,
        session_id: int,
        *,
        delivery_namespace: str | None = None,
        prompt: str | None = None,
        context: dict | None = None,
    ) -> Job:
        with self._transaction():
            self._conn.execute(
                "INSERT INTO jobs (id, session_id, status, created_at) VALUES (?, ?, ?, ?)",
                (job_id, session_id, JobStatus.RUNNING.value, _ts(utcnow())),
            )
            if prompt is not None:
                self._conn.execute(
                    "INSERT INTO job_prompts (job_id, prompt) VALUES (?, ?)",
                    (job_id, redact_text(prompt)),
                )
            if context is not None:
                self._conn.execute(
                    "INSERT INTO job_context (job_id, context) VALUES (?, ?)",
                    (job_id, json.dumps(redact_value(context))),
                )
            if delivery_namespace is not None:
                self._conn.execute(
                    "INSERT INTO outbound_deliveries (job_id, namespace) VALUES (?, ?)",
                    (job_id, delivery_namespace),
                )
        return self._require(self.job(job_id))

    def job_context(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT context FROM job_context WHERE job_id = ?", (job_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def observe_job_model(self, job_id: str, observation: dict | None) -> None:
        """Pin one native observation for this job; its source defines the phase."""
        from acp_gateway.agents.observations import observation as project

        with self._transaction():
            job = self.job(job_id)
            context = self.job_context(job_id)
            if job is None or job.status.finished or context is None:
                raise ValueError("native model snapshot requires an unfinished owned job")
            if context.get("native_model_pinned"):
                raise ValueError("native model snapshot is already pinned")
            value = observation or {}
            clean = project(value) or {}
            timestamp = value.get("observed_at")
            if not isinstance(timestamp, str) or len(timestamp) > 40:
                timestamp = None
            context.update(
                provider=clean.get("provider"),
                model=clean.get("model"),
                effort=clean.get("effort"),
                model_observation_source=clean.get("source"),
                model_observed_at=timestamp,
                native_model_pinned=True,
            )
            self._conn.execute(
                "UPDATE job_context SET context = ? WHERE job_id = ?",
                (json.dumps(context), job_id),
            )

    def dialogue_turn(self, session_id: int, job_id: str | None = None) -> dict:
        row = self._conn.execute(
            "SELECT j.id, j.status, j.answer, j.error, j.created_at, p.prompt FROM jobs j "
            "LEFT JOIN job_prompts p ON p.job_id = j.id WHERE j.session_id = ? "
            "AND (? IS NULL OR j.id = ?) ORDER BY j.created_at DESC, j.id DESC LIMIT 1",
            (session_id, job_id, job_id),
        ).fetchone()
        if row is None:
            return {"turn": None, "older": None, "newer": None}
        neighbors = {}
        for name, comparison, order in (("older", "<", "DESC"), ("newer", ">", "ASC")):
            neighbor = self._conn.execute(
                "SELECT id FROM jobs WHERE session_id = ? "  # noqa: S608
                f"AND (created_at, id) {comparison} (?, ?) ORDER BY created_at {order}, id {order} "
                "LIMIT 1",
                (session_id, row["created_at"], row["id"]),
            ).fetchone()
            neighbors[name] = neighbor["id"] if neighbor else None
        return {"turn": dict(row), **neighbors}

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

    def job_history_cursor(self, job_id, channel, key, agent=None):
        row = self._conn.execute(
            "SELECT j.created_at, j.id, s.agent FROM jobs j "
            "JOIN sessions s ON s.id=j.session_id "
            "WHERE j.id=? AND s.channel=? AND s.conversation_key=?",
            (job_id, channel, key),
        ).fetchone()
        if row is None or (agent is not None and row["agent"] != agent):
            return None
        return (_dt(row["created_at"]), row["id"])

    def job_history(self, channel, key, *, before=None, limit=11, active=False, agent=None):
        if type(limit) is not int or not 1 <= limit <= 51:
            raise ValueError("history limit must be between 1 and 51")
        where = ["s.channel = ?", "s.conversation_key = ?"]
        values = [channel, key]
        if agent is not None:
            where.append("s.agent = ?")
            values.append(agent)
        if active:
            where.append("j.status = 'running'")
        if before is not None:
            stamp, identity = before
            where.append("(j.created_at < ? OR (j.created_at = ? AND j.id < ?))")
            values += [_ts(stamp), _ts(stamp), identity]
        rows = self._conn.execute(
            "SELECT j.id, j.session_id, j.status, j.created_at, j.finished_at, "  # noqa: S608 - fixed clauses
            "s.channel, s.conversation_key, s.agent FROM jobs j "
            "JOIN sessions s ON s.id = j.session_id WHERE "
            + " AND ".join(where)
            + " ORDER BY j.created_at DESC, j.id DESC LIMIT ?",
            [*values, limit],
        )
        return [
            JobSummary(
                row["id"],
                row["session_id"],
                Conversation(row["channel"], row["conversation_key"], row["agent"]),
                JobStatus(row["status"]),
                _dt(row["created_at"]),
                _dt(row["finished_at"]),
            )
            for row in rows
        ]

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

    def prune_jobs(self, finished_before: datetime, *, limit: int = 1000) -> int:
        """Delete at most one batch of finished jobs, keeping live jobs and sessions."""
        self._check_prune_limit(limit)
        cursor = self._conn.execute(
            "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs "
            "WHERE status != ? AND finished_at < ? ORDER BY finished_at, id LIMIT ?)",
            (JobStatus.RUNNING.value, _ts(finished_before), limit),
        )
        return cursor.rowcount

    def prune_approval_audit(self, resolved_before: datetime, *, limit: int = 1000) -> int:
        """Expire settled decisions only; pending approvals are never stored here."""
        self._check_prune_limit(limit)
        cursor = self._conn.execute(
            "DELETE FROM approvals_audit WHERE id IN (SELECT id FROM approvals_audit "
            "WHERE resolved_at < ? ORDER BY resolved_at, id LIMIT ?)",
            (_ts(resolved_before), limit),
        )
        return cursor.rowcount

    @staticmethod
    def _check_prune_limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("retention batch limit must be 1..10000")

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
        if session_id is not None:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND channel = ? "
                "AND conversation_key = ? AND agent = ?",
                (session_id, *self._key(conversation)),
            ).fetchone()
            if row is None:
                raise ValueError("this conversation does not own that dialogue")
        revision = self.active_revision(conversation)
        self._conn.execute(
            "INSERT INTO conversations (channel, conversation_key, agent, active_session_id)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (channel, conversation_key, agent)"
            " DO UPDATE SET active_session_id = excluded.active_session_id",
            (*self._key(conversation), session_id),
        )
        if self.schema_version >= 3:
            key = json.dumps(self._key(conversation), separators=(",", ":"))
            self.set_channel_state("active_selection_revision", key, revision + 1)

    @staticmethod
    def _require[R](record: R | None) -> R:
        if record is None:
            raise LookupError("a row written in this call is missing")
        return record

    @staticmethod
    def _key(conversation: Conversation) -> tuple[str, str, str]:
        return (conversation.channel, conversation.key, conversation.agent)

    def _session(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            conversation=Conversation(row["channel"], row["conversation_key"], row["agent"]),
            acp_session_id=row["acp_session_id"],
            cwd=row["cwd"],
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used_at=datetime.fromisoformat(row["last_used_at"]),
            binding=self.channel_state("agent_session_binding", str(row["id"]))
            if self.schema_version >= 3
            else None,
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
