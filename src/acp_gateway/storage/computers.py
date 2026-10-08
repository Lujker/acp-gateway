"""Local enrollment; raw credentials are never stored in the database."""

import hashlib
import hmac
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

    def authenticate(self, computer_id: str, credential: str) -> bool:
        return self.authorize(computer_id, credential) is not None

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
        return row[2] if row and row[1] and matches else None

    def grant_valid(self, computer_id: str, generation: int) -> bool:
        row = self._conn.execute(
            "SELECT enabled, generation FROM computers WHERE computer_id = ?", (computer_id,)
        ).fetchone()
        return bool(row and row[0] and row[1] == generation)

    def issue(self, computer_id: str, token_file: Path, *, display_name: str | None = None):
        """Enroll with a name, otherwise rotate. Export to a new private file.

        BEGIN IMMEDIATE serializes issuance/revocation across CLI processes.
        Roll back and remove our file if writing or committing fails.
        """
        _validate_id(computer_id)
        if display_name is not None and not 1 <= len(display_name.strip()) <= 128:
            raise ValueError("computer display name must contain 1-128 characters")
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
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = os.fstat(fd)
            if os.name == "posix":
                os.fchmod(fd, 0o600)
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
            "UPDATE computers SET enabled = 0, updated_at = ? WHERE computer_id = ?",
            (utcnow().isoformat(), computer_id),
        )
        if not cursor.rowcount:
            raise ValueError("computer is unknown")
        self._changed(computer_id)
