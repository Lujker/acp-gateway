"""Local native-session provenance, exclusive ownership and durable unknown-turn fences.

Only IDs created by this profile can be loaded. An OS lock excludes other gateway
processes; a committed busy marker survives a crash. No prompts or credentials are
stored here, and releasing a lease never clears an unknown turn by itself: only a
confirmed process stop turns it into ``interrupted``, which the owner may resume
explicitly. The interrupted prompt is never replayed. When even the stop was
never confirmed (crash, unconfirmed WSL cleanup), the binding stays fenced until
the owner explicitly releases it after checking that the work has stopped.
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


class InterruptedTurn(LocalPolicyError):
    """The last turn's outcome is unknown; only an explicit resume may continue."""


class FencedBinding(LocalPolicyError):
    """Earlier cleanup or a turn outcome was never confirmed; the owner may release it."""


# Persisted states a confirmed stop would have cleared, mapped to what it would set.
FENCED = {"attached": "idle", "empty-attached": "empty-idle", "busy": "interrupted"}


class ManagedSessions:
    def __init__(self, directory, profile):
        self.directory, self.profile = directory, profile
        self._locks = {}
        settings = getattr(profile, profile.kind)
        wsl = settings.wsl
        # Where native chats live and under which permissions, not how the CLI is
        # launched: reinstalling the CLI or changing PATH must not orphan dialogues.
        identity = {
            "kind": profile.kind,
            "profile": profile.alias,
            "cwd": profile.default_cwd,
            "mode": settings.mode,
            "wsl": (
                {
                    "distribution": wsl.distribution,
                    "user": wsl.user,
                    "home": wsl.home,
                    "agent_home": wsl.agent_home,
                }
                if wsl
                else None
            ),
            "home": None if wsl else str(Path.home()),
            "agent_home": None
            if wsl
            else os.environ.get("CODEX_HOME" if profile.kind == "codex" else "CLAUDE_CONFIG_DIR"),
            "version": 2,
        }
        self.scope = self._hash(identity)
        # Bindings written before 0.2.9 also hashed the executable and WSL PATH/python.
        legacy = {key: identity[key] for key in identity if key != "version"}
        legacy |= {"executable": settings.executable, "wsl": wsl.model_dump() if wsl else None}
        self.legacy_scope = self._hash(legacy)

    @staticmethod
    def _hash(identity):
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def _migrate(self, db, sid=None):
        """Move legacy rows of this exact profile to the stable scope (idempotent)."""
        if sid is None:
            db.execute(
                "UPDATE OR IGNORE bindings SET scope=? WHERE scope=?",
                (self.scope, self.legacy_scope),
            )
        else:
            db.execute(
                "UPDATE OR IGNORE bindings SET scope=? WHERE scope=? AND sid=?",
                (self.scope, self.legacy_scope, sid),
            )

    def fork(self):
        return type(self)(self.directory, self.profile)

    def contains(self, sid, cwd):
        """Check provenance without attaching a native process or clearing a busy fence."""
        if not isinstance(sid, str) or not sid or len(sid) > 128 or not sid.isprintable():
            return False
        path = self.directory / "bindings.sqlite3"
        if not path.is_file():
            return False
        with closing(
            sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            row = db.execute(
                "SELECT cwd FROM bindings WHERE scope IN (?,?) AND sid=?",
                (self.scope, self.legacy_scope, sid),
            ).fetchone()
        return row == (cwd,)

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

    def load(self, sid, cwd, *, resume_interrupted=False, release_fence=False):
        self._acquire(sid)
        try:
            with closing(self._db()) as db, db:
                self._migrate(db, sid)
                record = db.execute(
                    "SELECT cwd,state FROM bindings WHERE scope=? AND sid=?", (self.scope, sid)
                ).fetchone()
                if record is not None and record[0] == cwd and record[1] in FENCED:
                    # Our OS lock is held, so no process of this installation owns it.
                    if not release_fence:
                        raise FencedBinding(
                            "native binding fenced: earlier outcome unknown; release explicitly"
                        )
                    # The owner confirmed the earlier work stopped. An unknown turn is
                    # continued like an interrupted one: its prompt is never replayed.
                    record = (cwd, FENCED[record[1]])
                    resume_interrupted = True
                if record == (cwd, "interrupted") and not resume_interrupted:
                    raise InterruptedTurn("native turn was interrupted; resume explicitly")
                allowed = {(cwd, "idle"), (cwd, "empty-idle")}
                if resume_interrupted:
                    allowed.add((cwd, "interrupted"))
                if record not in allowed:
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
                # An interrupted turn already reached the CLI, so history exists.
                return record[1] in {"idle", "interrupted"}
        except BaseException:
            self._locks.pop(sid).release()
            raise

    def fenced(self):
        """Fenced bindings of this exact profile scope: (sid, cwd, state)."""
        path = self.directory / "bindings.sqlite3"
        if not path.is_file():
            return []
        with closing(
            sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            rows = db.execute(
                "SELECT sid, cwd, state FROM bindings WHERE scope IN (?,?) AND state IN (?,?,?)"
                " ORDER BY sid",
                (self.scope, self.legacy_scope, *FENCED),
            ).fetchall()
        return [row for row in rows if not self._held(row[0])]

    def _held(self, sid):
        """True while another process of this installation owns the binding's lock."""
        name = hashlib.sha256(sid.encode()).hexdigest() + ".lock"
        lock = FileLock(self.directory / name, thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return True
        lock.release()
        return False

    def release(self, sid):
        """Owner-confirmed offline release: the binding becomes idle, nothing replayed."""
        rows = {row[0]: row for row in self.fenced()}
        if sid not in rows:
            raise LocalPolicyError("no fenced binding with that ID in this profile")
        self.load(sid, rows[sid][1], release_fence=True)
        self.close()

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

    def close(self, *, confirmed=True, interrupted=False):
        # Persisted 'attached' is already a fence if a process crashes, disk
        # writes fail, or WSL cleanup cannot prove termination. Only a confirmed
        # stop may make idle sessions available again. ``interrupted`` (also only
        # after a confirmed stop) marks unknown turns as explicitly resumable;
        # busy is never cleared silently.
        if confirmed and self._locks:
            with closing(self._db()) as db, db:
                db.executemany(
                    "UPDATE bindings SET state=CASE state WHEN 'empty-attached' "
                    "THEN 'empty-idle' WHEN 'busy' THEN 'interrupted' ELSE 'idle' END "
                    "WHERE scope=? AND sid=? AND state IN ('attached','empty-attached',?)",
                    [
                        (self.scope, sid, "busy" if interrupted else "attached")
                        for sid in self._locks
                    ],
                )
        for lock in self._locks.values():
            lock.release()
        self._locks.clear()
