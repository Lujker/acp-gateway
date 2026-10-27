"""Local, durable at-most-once shell intents; never store an executable replay payload."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from uuid import UUID

from acp_gateway.connectors.policy import LocalPolicyError, _cwd
from acp_gateway.log import redact_text

ACTIVE = ("reserved", "running")
TERMINAL = ("completed", "failed", "cancelled", "timed_out", "unknown")


def identity(operation):
    try:
        return UUID(str(operation)).hex
    except (ValueError, TypeError, AttributeError):
        raise LocalPolicyError("invalid shell operation identity") from None


class ShellJournal:
    def __init__(self, path, *, max_records=100_000, max_running=4):
        if type(max_records) is not int or not 1 <= max_records <= 100_000:
            raise ValueError("invalid shell journal capacity")
        if type(max_running) is not int or not 1 <= max_running <= 32:
            raise ValueError("invalid shell concurrency limit")
        self.max_records, self.max_running = max_records, max_running
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            parent = path.parent.stat()
            if path.parent.is_symlink() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise ValueError("shell journal directory must be private to this user")
        elif os.name == "nt":
            from acp_gateway.windows_acl import ensure_private_directory

            ensure_private_directory(path.parent.absolute())
        if path.is_symlink():
            raise ValueError("shell journal must not be a symlink")
        path.touch(mode=0o600, exist_ok=True)
        if os.name == "posix":
            for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                if item.exists():
                    item.chmod(0o600)
        self.conn = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, digest TEXT NOT NULL,
                    alias TEXT NOT NULL, session_id TEXT NOT NULL, cwd TEXT NOT NULL,
                    command TEXT NOT NULL, timeout REAL NOT NULL,
                    state TEXT NOT NULL, shell TEXT NOT NULL DEFAULT '',
                    output TEXT NOT NULL DEFAULT '', exit_code INTEGER,
                    output_omitted INTEGER NOT NULL DEFAULT 0,
                    cleanup_verified INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS shell_states ON operations(state, updated);
            """)
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def get(self, operation, *, actor):
        row = self.conn.execute(
            "SELECT * FROM operations WHERE id = ? AND actor = ?", (identity(operation), actor)
        ).fetchone()
        if row is None:
            raise LocalPolicyError("shell operation unavailable for this actor")
        return dict(row)

    def reserve(self, operation, *, actor, alias, session_id, cwd, command, timeout=120):
        operation = identity(operation)
        if not isinstance(actor, str) or not re.fullmatch(r"telegram:[0-9]{1,20}", actor):
            raise LocalPolicyError("invalid shell actor")
        if not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", alias):
            raise LocalPolicyError("invalid shell alias")
        if (
            not isinstance(session_id, str)
            or not 1 <= len(session_id) <= 128
            or not session_id.isprintable()
        ):
            raise LocalPolicyError("invalid shell session")
        cwd = _cwd(cwd)
        if (
            not isinstance(command, str)
            or not command.strip()
            or len(command) > 4096
            or "\x00" in command
        ):
            raise LocalPolicyError("invalid shell command")
        if type(timeout) not in {int, float} or not 0 < timeout <= 600:
            raise LocalPolicyError("invalid shell timeout")
        timeout = float(timeout)
        digest = hashlib.sha256(
            json.dumps(
                [actor, alias, session_id, cwd, command, timeout],
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            previous = self.conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation,)
            ).fetchone()
            if previous is not None:
                if previous["digest"] != digest or previous["actor"] != actor:
                    raise LocalPolicyError("shell operation identity changed")
                self.conn.execute("COMMIT")
                return False, dict(previous)
            count = self.conn.execute("SELECT count(*) FROM operations").fetchone()[0]
            if count >= self.max_records:
                raise LocalPolicyError("shell operation journal capacity reached")
            live = self.conn.execute(
                "SELECT count(*) FROM operations WHERE state != 'expired'"
            ).fetchone()[0]
            if live >= min(self.max_records, 1024):
                raise LocalPolicyError("shell result storage capacity reached")
            active = self.conn.execute(
                "SELECT count(*) FROM operations WHERE state IN ('reserved', 'running')"
            ).fetchone()[0]
            if active >= self.max_running:
                raise LocalPolicyError("shell concurrency limit reached")
            self.conn.execute(
                "INSERT INTO operations (id,actor,digest,alias,session_id,cwd,command,timeout,"
                "state,created,updated) VALUES (?,?,?,?,?,?,?,?,'reserved',?,?)",
                (
                    operation,
                    actor,
                    digest,
                    alias,
                    session_id,
                    cwd,
                    redact_text(command),
                    timeout,
                    now,
                    now,
                ),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return True, self.get(operation, actor=actor)

    def running(self, operation, *, actor, interpreter):
        if interpreter not in {"sh", "bash (WSL)", "Windows PowerShell"}:
            raise LocalPolicyError("invalid shell implementation")
        self.get(operation, actor=actor)
        if (
            self.conn.execute(
                "UPDATE operations SET state='running', shell=?, updated=? "
                "WHERE id=? AND actor=? AND state='reserved'",
                (interpreter, time.time(), identity(operation), actor),
            ).rowcount
            != 1
        ):
            raise LocalPolicyError("shell operation is not reserved")

    def finish(
        self,
        operation,
        *,
        actor,
        state,
        output="",
        exit_code=None,
        output_omitted=False,
        cleanup_verified=False,
        reason="",
    ):
        if state not in TERMINAL or not isinstance(output, str) or len(output) > 65_536:
            raise LocalPolicyError("invalid shell result")
        if type(output_omitted) is not bool or type(cleanup_verified) is not bool:
            raise LocalPolicyError("invalid shell result flags")
        if state == "completed" and exit_code != 0:
            raise LocalPolicyError("successful shell result requires exit code zero")
        if exit_code is not None and (
            type(exit_code) is not int or not -(2**31) <= exit_code < 2**32
        ):
            raise LocalPolicyError("invalid shell exit code")
        if state != "unknown" and cleanup_verified is not True:
            raise LocalPolicyError("shell cleanup is unverified")
        if reason not in {
            "",
            "spawn_failed",
            "execution_failed",
            "cleanup_unverified",
            "interrupted",
            "output_limit",
        }:
            raise LocalPolicyError("invalid shell result reason")
        clean_output = redact_text(output)
        if len(clean_output) > 65_536:
            # Redaction may expand short assignments (key=x -> key=***).
            # Never truncate a masked result into an incoherent RPC window.
            clean_output, output_omitted, reason = "", True, "output_limit"
        self.get(operation, actor=actor)
        if (
            self.conn.execute(
                "UPDATE operations SET state=?,output=?,exit_code=?,output_omitted=?,"
                "cleanup_verified=?,"
                "reason=?,updated=? WHERE id=? AND actor=? AND state IN ('reserved','running')",
                (
                    state,
                    clean_output,
                    exit_code,
                    int(output_omitted),
                    int(cleanup_verified),
                    reason,
                    time.time(),
                    identity(operation),
                    actor,
                ),
            ).rowcount
            != 1
        ):
            raise LocalPolicyError("shell operation already settled")
        return self.get(operation, actor=actor)

    def recover(self):
        """Called once by the exclusive local executor, never by a status reader."""
        return self.conn.execute(
            "UPDATE operations SET state='unknown',reason='interrupted',updated=? "
            "WHERE state IN ('reserved','running')",
            (time.time(),),
        ).rowcount

    def expire(self, *, before):
        # Keep UUID tombstones: deleting old IDs would permit their execution again.
        return self.conn.execute(
            "UPDATE operations SET state='expired', command='',output='',cwd='',alias='',"
            "session_id='',"
            "shell='',updated=? WHERE cleanup_verified=1 AND updated < ? "
            "AND state IN ('completed','failed','cancelled','timed_out','unknown')",
            (time.time(), before),
        ).rowcount

    def activity(self):
        row = self.conn.execute(
            "SELECT sum(state IN ('reserved','running')), "
            "sum(state='unknown' AND cleanup_verified=0) FROM operations"
        ).fetchone()
        return {"running_sessions": row[0] or 0, "uncertain_sessions": row[1] or 0}
