"""Local native-session provenance, exclusive ownership and durable unknown-turn fences.

Only IDs created by this profile can be loaded. An OS lock excludes other gateway
processes; a committed busy marker survives a crash. No prompts or credentials are
stored here, and releasing a lease never clears an unknown turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from filelock import FileLock, Timeout

from acp_gateway.connectors.policy import LocalPolicyError


class ManagedSessions:
    def __init__(self, directory, profile):
        self.directory, self.profile = directory, profile
        self._locks = {}
        settings = getattr(profile, profile.kind)
        identity = {
            "kind": profile.kind,
            "profile": profile.alias,
            "cwd": profile.default_cwd,
            "mode": settings.mode,
            "executable": settings.executable,
            "wsl": settings.wsl.model_dump() if settings.wsl else None,
            "home": None if settings.wsl else str(Path.home()),
            "agent_home": None
            if settings.wsl
            else os.environ.get("CODEX_HOME" if profile.kind == "codex" else "CLAUDE_CONFIG_DIR"),
        }
        self.scope = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def fork(self):
        return type(self)(self.directory, self.profile)

    def _db(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.directory / "bindings.sqlite3", timeout=2)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bindings "
                "(scope TEXT, sid TEXT, cwd TEXT NOT NULL, state TEXT NOT NULL, "
                "PRIMARY KEY(scope, sid))"
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def _acquire(self, sid):
        if not isinstance(sid, str) or not sid or len(sid) > 128 or not sid.isprintable():
            raise LocalPolicyError("invalid native session identity")
        if sid in self._locks:
            raise LocalPolicyError("native session already attached")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(sid.encode()).hexdigest() + ".lock"
        lock = FileLock(self.directory / name, thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise LocalPolicyError("native session already attached") from None
        self._locks[sid] = lock

    def create(self, sid, cwd, *, empty=False):
        if empty and self.profile.kind != "claude_code":
            raise LocalPolicyError("empty native bindings require Claude Code")
        self._acquire(sid)
        try:
            with closing(self._db()) as db, db:
                db.execute(
                    "INSERT INTO bindings VALUES (?, ?, ?, ?)",
                    (self.scope, sid, cwd, "empty-attached" if empty else "attached"),
                )
        except BaseException:
            self._locks.pop(sid).release()
            raise

    def load(self, sid, cwd):
        self._acquire(sid)
        try:
            with closing(self._db()) as db, db:
                record = db.execute(
                    "SELECT cwd,state FROM bindings WHERE scope=? AND sid=?", (self.scope, sid)
                ).fetchone()
                if record not in ((cwd, "idle"), (cwd, "empty-idle")):
                    raise LocalPolicyError(
                        "native binding is unavailable or turn outcome is unknown"
                    )
                db.execute(
                    "UPDATE bindings SET state=? WHERE scope=? AND sid=?",
                    (
                        "empty-attached" if record[1] == "empty-idle" else "attached",
                        self.scope,
                        sid,
                    ),
                )
                # Legacy idle bindings conservatively require real native history.
                return record[1] == "idle"
        except BaseException:
            self._locks.pop(sid).release()
            raise

    def _state(self, sid, before, after, *, alternative=None):
        if sid not in self._locks:
            raise LocalPolicyError("native binding is not owned")
        with closing(self._db()) as db, db:
            changed = db.execute(
                "UPDATE bindings SET state=? WHERE scope=? AND sid=? AND state IN (?,?)",
                (after, self.scope, sid, before, alternative or before),
            ).rowcount
            if changed != 1:
                raise LocalPolicyError("native turn outcome is unknown; no automatic replay")

    def begin(self, sid):
        # Persist the busy fence BEFORE any first prompt may reach the native CLI.
        self._state(sid, "attached", "busy", alternative="empty-attached")

    def finish(self, sid):
        self._state(sid, "busy", "attached")

    def close(self, *, confirmed=True):
        # Persisted 'attached' is already a fence if a process crashes, disk
        # writes fail, or WSL cleanup cannot prove termination. Only a confirmed
        # stop may make idle sessions available again; busy is never cleared.
        if confirmed and self._locks:
            with closing(self._db()) as db, db:
                db.executemany(
                    "UPDATE bindings SET state=CASE state WHEN 'empty-attached' "
                    "THEN 'empty-idle' ELSE 'idle' END WHERE scope=? AND sid=? "
                    "AND state IN ('attached','empty-attached')",
                    [(self.scope, sid) for sid in self._locks],
                )
        for lock in self._locks.values():
            lock.release()
        self._locks.clear()
