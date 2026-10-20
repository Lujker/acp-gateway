"""Local enrollment; raw credentials are never stored in the database."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

from acp_gateway.storage.records import utcnow

_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_PUBLIC = "computer_id, display_name, enabled, generation, created_at, updated_at"


def _digest(credential: str) -> str:
    return hashlib.sha256(b"acpgw-computer-v1\0" + credential.encode("utf-8")).hexdigest()


def _validate_id(computer_id: str) -> None:
    if not _ID.fullmatch(computer_id):
        raise ValueError("invalid computer ID: use 1-64 lowercase letters, digits, _ or -")


class ComputerRegistry:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._listeners = []

    def subscribe(self, callback):
        """Notify in-process consumers after a committed access change."""
        self._listeners.append(callback)

    def _changed(self, computer_id):
        for callback in self._listeners:
            callback(computer_id)

    def list(self) -> list[dict]:
        rows = self._conn.execute(f"SELECT {_PUBLIC} FROM computers ORDER BY computer_id")  # noqa: S608
        return [dict(row) for row in rows]

    def connection_status(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT c.computer_id, c.display_name, c.enabled, "
            "s.epoch, s.connected_at, s.disconnected_at, s.disconnect_reason, s.close_code, "
            "s.agents_json FROM computers c LEFT JOIN computer_connections s "
            "ON s.computer_id = c.computer_id ORDER BY c.computer_id"
        )
        result = []
        for row in rows:
            value = dict(row)
            value["agents"] = json.loads(value.pop("agents_json") or "[]")
            value["enabled"] = bool(value["enabled"])
            result.append(value)
        return result

    def connection_opened(self, computer_id, epoch, aliases):
        self._conn.execute(
            "INSERT INTO computer_connections "
            "(computer_id, epoch, connected_at, agents_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(computer_id) DO UPDATE SET epoch = excluded.epoch, "
            "connected_at = excluded.connected_at, agents_json = excluded.agents_json, "
            "disconnected_at = NULL, disconnect_reason = NULL, close_code = NULL",
            (computer_id, str(epoch), utcnow().isoformat(), json.dumps(aliases)),
        )

    def connection_closed(self, computer_id, epoch, reason, close_code=None):
        # Retired handlers must never mark a newer epoch as disconnected.
        self._conn.execute(
            "UPDATE computer_connections SET disconnected_at = ?, disconnect_reason = ?, "
            "close_code = ? WHERE computer_id = ? AND epoch = ? AND disconnected_at IS NULL",
            (utcnow().isoformat(), reason, close_code, computer_id, str(epoch)),
        )

    def settle_connections(self):
        """A persisted open record cannot mean online after the listener restarts."""
        self._conn.execute(
            "UPDATE computer_connections SET disconnected_at = ?, "
            "disconnect_reason = 'listener_restarted' WHERE disconnected_at IS NULL",
            (utcnow().isoformat(),),
        )

    def migration_required(self, computer_id, credential):
        """Reveal the migration hint only to a holder of a valid retired legacy key."""
        if not isinstance(credential, str) or len(credential) > 256 or not credential.isascii():
            return False
        boundary = self._conn.execute(
            "SELECT value FROM channel_state WHERE namespace='pairing' AND key='boundary'"
        ).fetchone()
        if boundary is None:
            return False
        row = self._conn.execute(
            "SELECT credential_digest FROM computers WHERE computer_id=? AND computer_id NOT "
            "IN (SELECT computer_id FROM pairing_bindings)",
            (computer_id,),
        ).fetchone()
        return bool(row and hmac.compare_digest(row[0], _digest(credential)))

    def authenticate(self, computer_id: str, credential: str) -> bool:
        return self.authorize(computer_id, credential) is not None

    def _boundary_active(self):
        state = self._conn.execute(
            "SELECT 1 FROM channel_state WHERE namespace='pairing' AND key='boundary'"
        ).fetchone()
        if state is not None:
            return True
        db = self._conn.execute("PRAGMA database_list").fetchone()[2]
        return bool(db and (Path(db).parent / "pairing-boundary.json").exists())

    def _pairing_allowed(self, computer_id, generation):
        db = self._conn.execute("PRAGMA database_list").fetchone()[2]
        boundary = Path(db).parent / "pairing-boundary.json" if db else None
        state = self._conn.execute(
            "SELECT value FROM channel_state WHERE namespace='pairing' AND key='boundary'"
        ).fetchone()
        if boundary is None or not boundary.exists():
            return state is None
        try:
            epoch = json.loads(boundary.read_text())["epoch"]
            binding = self._conn.execute(
                "SELECT generation FROM pairing_bindings WHERE computer_id=?", (computer_id,)
            ).fetchone()
            return bool(
                state and json.loads(state[0]) == epoch and binding and binding[0] == generation
            )
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return False

    def authorize(self, computer_id: str, credential: str) -> int | None:
        """Return a generation grant; rotation/revocation invalidates that grant."""
        if not _ID.fullmatch(computer_id) or len(credential) > 256 or not credential.isascii():
            return None
        row = self._conn.execute(
            "SELECT credential_digest, enabled, generation FROM computers WHERE computer_id = ?",
            (computer_id,),
        ).fetchone()
        expected = row[0] if row else "0" * 64
        matches = hmac.compare_digest(expected, _digest(credential))
        return (
            row[2]
            if row and row[1] and matches and self._pairing_allowed(computer_id, row[2])
            else None
        )

    def grant_valid(self, computer_id: str, generation: int) -> bool:
        row = self._conn.execute(
            "SELECT enabled, generation FROM computers WHERE computer_id = ?", (computer_id,)
        ).fetchone()
        return bool(
            row
            and row[0]
            and row[1] == generation
            and self._pairing_allowed(computer_id, generation)
        )

    def issue(self, computer_id: str, token_file: Path, *, display_name: str | None = None):
        """Enroll with a name, otherwise rotate. Export to a new private file.

        BEGIN IMMEDIATE serializes issuance/revocation across CLI processes.
        Roll back and remove our file if writing or committing fails.
        """
        _validate_id(computer_id)
        if display_name is not None and not 1 <= len(display_name.strip()) <= 128:
            raise ValueError("computer display name must contain 1-128 characters")
        if os.name == "nt":
            from acp_gateway.windows_acl import check_private_paths

            # New files inherit the parent DACL. Refuse public inheritance before
            # creating a file or writing any bearer bytes; never repair user ACLs.
            parent = token_file.absolute().parent
            check_private_paths(
                [parent], directories=[parent], purpose="Computer credential export"
            )
        token = "acpc_" + secrets.token_urlsafe(32)
        created = None
        fd = None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT enabled FROM computers WHERE computer_id = ?", (computer_id,)
            ).fetchone()
            if display_name is not None and row:
                raise ValueError("computer already enrolled")
            if display_name is None and (not row or not row[0]):
                raise ValueError("computer is unknown or revoked")
            binding = None
            if self._boundary_active():
                binding = self._conn.execute(
                    "SELECT 1 FROM pairing_bindings WHERE computer_id=?", (computer_id,)
                ).fetchone()
                if binding is None:
                    raise ValueError(
                        "pairing is required after migration; run acpgw connect to enroll "
                        "or re-pair this computer"
                    )
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = os.fstat(fd)
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            elif os.name == "nt":
                check_private_paths(
                    [token_file.absolute()],
                    directories=[parent],
                    purpose="Computer credential export",
                )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = None
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            now = utcnow().isoformat()
            if display_name is not None:
                self._conn.execute(
                    "INSERT INTO computers (computer_id, display_name, credential_digest, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (computer_id, display_name.strip(), _digest(token), now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE computers SET credential_digest = ?, generation = generation + 1, "
                    "updated_at = ? WHERE computer_id = ?",
                    (_digest(token), now, computer_id),
                )
                if binding is not None:
                    # Rotation is a local administrative act, but a paired
                    # computer must keep the same owner binding generation.
                    self._conn.execute(
                        "UPDATE pairing_bindings SET generation = generation + 1 "
                        "WHERE computer_id = ?",
                        (computer_id,),
                    )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if fd is not None:
                os.close(fd)
            if created is not None:
                try:
                    current = token_file.lstat()
                    if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                        token_file.unlink()
                except FileNotFoundError:
                    pass
            raise
        self._changed(computer_id)

    def revoke(self, computer_id: str) -> None:
        _validate_id(computer_id)
        cursor = self._conn.execute(
            "UPDATE computers SET enabled = 0, generation = generation + 1, updated_at = ? "
            "WHERE computer_id = ?",
            (utcnow().isoformat(), computer_id),
        )
        if not cursor.rowcount:
            raise ValueError("computer is unknown")
        self._changed(computer_id)
