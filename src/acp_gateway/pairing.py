"""Connector-first enrollment inspired by RFC 8628, not an OAuth implementation.

A 256-bit device_code is a bearer over TLS, never a public confirmation code.
Only the private, short-lived issuance row contains a recoverable credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from acp_gateway.log import register_secret
from acp_gateway.storage.computers import _digest
from acp_gateway.storage.records import utcnow


class PairingService:
    def __init__(self, store, *, boundary: Path, clock=time.time, ttl=600, capacity=256):
        self.store, self.conn = store, store._conn
        self.boundary, self.clock = boundary, clock
        self.ttl, self.capacity = ttl, capacity
        if not boundary.exists() and store.channel_state("pairing", "boundary") is not None:
            raise ValueError(
                "pairing rollback boundary missing; restore the external boundary before starting"
            )
        if boundary.exists():
            try:
                epoch = json.loads(boundary.read_text())["epoch"]
                if store.channel_state("pairing", "boundary") != epoch:
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError(
                    "pairing rollback boundary mismatch; restore current DB, not legacy authority"
                ) from None

    def _transaction(self):
        # Claim/ACK/revoke writers serialize across independent CLI/daemon processes.
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def bootstrap(self, telegram_ids):
        ids = sorted(set(telegram_ids))
        if any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError("approvers must be explicit positive Telegram IDs")
        with self._transaction():
            row = self.conn.execute("SELECT * FROM pairing_owner WHERE singleton=1").fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO pairing_owner VALUES (1, ?, ?)",
                    ("owner_" + secrets.token_hex(16), json.dumps(ids)),
                )
            elif json.loads(row["telegram_ids"]) != ids:
                raise ValueError("owner already bootstrapped with different approvers")
        return self.owner("local")

    def owner(self, actor):
        row = self.conn.execute("SELECT * FROM pairing_owner WHERE singleton=1").fetchone()
        if row is None or (
            actor != "local"
            and actor not in {f"telegram:{i}" for i in json.loads(row["telegram_ids"])}
        ):
            raise ValueError("owner authorization required")
        return {"owner_id": row["owner_id"], "telegram_ids": json.loads(row["telegram_ids"])}

    @staticmethod
    def _applicant(secret):
        if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
            raise ValueError("invalid applicant secret")
        return hashlib.sha256(secret.encode()).hexdigest()

    def _cleanup(self):
        # An issued credential may outlive its request window when the ACK was
        # lost. Keep it recoverable; deleting it would orphan a disabled computer
        # and force a duplicate enrollment on the next attempt.
        self.conn.execute(
            "DELETE FROM pairing_requests WHERE expires <= ? AND state IN "
            "('pending', 'approved', 'denied', 'cancelled')",
            (self.clock(),),
        )

    def cleanup(self):
        with self._transaction():
            self._cleanup()

    @staticmethod
    def _public(row):
        return {
            key: row[key]
            for key in ("request_id", "name", "target", "verification", "expires", "state")
        }

    def request(self, owner_id, device_code, name, target=None):
        digest = self._applicant(device_code)
        if not isinstance(owner_id, str) or len(owner_id) > 80:
            raise ValueError("invalid request")
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or any(ord(c) < 32 for c in name):
            raise ValueError("invalid computer name")
        if target is not None and (
            not isinstance(target, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", target)
        ):
            raise ValueError("invalid rebind target")
        with self._transaction():
            self._cleanup()
            old = self.conn.execute(
                "SELECT * FROM pairing_requests WHERE applicant_digest=?", (digest,)
            ).fetchone()
            if old:
                if (old["owner_id"], old["name"], old["target"]) != (owner_id, name, target):
                    raise ValueError("pending request metadata changed; cancel locally first")
                return self._public(old)
            if (
                self.conn.execute("SELECT count(*) FROM pairing_requests").fetchone()[0]
                >= self.capacity
            ):
                raise ValueError("pairing capacity reached; retry later")
            row = self.conn.execute(
                "SELECT generation FROM computers WHERE computer_id=?", (target,)
            ).fetchone()
            revision = row[0] if row else None
            request_id = secrets.token_hex(16)
            metadata = json.dumps(
                [request_id, owner_id, name, target, revision], separators=(",", ":")
            )
            verification = hashlib.sha256(metadata.encode()).hexdigest()[:24].upper()
            self.conn.execute(
                "INSERT INTO pairing_requests "
                "(request_id,owner_id,applicant_digest,name,target,target_generation,"
                "verification,expires,state) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    owner_id,
                    digest,
                    name,
                    target,
                    revision,
                    verification,
                    self.clock() + self.ttl,
                    "pending",
                ),
            )
            return self._public(
                self.conn.execute(
                    "SELECT * FROM pairing_requests WHERE request_id=?", (request_id,)
                ).fetchone()
            )

    def pending(self, actor):
        owner = self.owner(actor)
        self._cleanup()
        return [
            self._public(r)
            for r in self.conn.execute(
                "SELECT * FROM pairing_requests WHERE owner_id=? AND state='pending' ORDER "
                "BY expires",
                (owner["owner_id"],),
            )
        ]

    def _row(self, request_id, *, allow_terminal=False):
        row = self.conn.execute(
            "SELECT * FROM pairing_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        terminal = row is not None and row["state"] in {
            "issued",
            "active",
            "denied",
            "cancelled",
        }
        if not row or (row["expires"] <= self.clock() and not (allow_terminal and terminal)):
            raise ValueError("pairing request unavailable or expired")
        return row

    def decide(self, actor, request_id, verification, *, approve):
        owner = self.owner(actor)
        if not isinstance(verification, str) or not re.fullmatch(r"[0-9A-F]{24}", verification):
            raise ValueError("request comparison failed")
        with self._transaction():
            row = self._row(request_id)
            if row["owner_id"] != owner["owner_id"] or not hmac.compare_digest(
                row["verification"], verification
            ):
                raise ValueError("request comparison failed")
            state = "approved" if approve else "denied"
            if row["state"] == state or (approve and row["state"] in {"issued", "active"}):
                return self._public(row)
            if row["state"] != "pending":
                raise ValueError("request already resolved")
            if row["target"]:
                self._target_owner(row)
                target = self.conn.execute(
                    "SELECT generation FROM computers WHERE computer_id=?", (row["target"],)
                ).fetchone()
                if target is None or target[0] != row["target_generation"]:
                    raise ValueError("rebind target changed; create a new request")
            self.conn.execute(
                "UPDATE pairing_requests SET state=? WHERE request_id=?", (state, request_id)
            )
            self._audit(request_id, state, actor)
            return {**self._public(row), "state": state}

    def _bound(self, request_id, device_code, *, allow_terminal=False):
        digest = self._applicant(device_code)
        row = self._row(request_id, allow_terminal=allow_terminal)
        if not hmac.compare_digest(row["applicant_digest"], digest):
            raise ValueError("pairing request unavailable or expired")
        return row

    def claim(self, request_id, device_code):
        with self._transaction():
            row = self._bound(request_id, device_code, allow_terminal=True)
            if row["state"] in {"pending", "denied", "cancelled"}:
                return {"status": row["state"]}
            if row["state"] == "approved":
                computer_id = row["target"] or "pc_" + secrets.token_hex(12)
                token = "acpc_" + secrets.token_urlsafe(32)
                now = utcnow().isoformat()
                if row["target"]:
                    self._target_owner(row)
                    current = self.conn.execute(
                        "SELECT generation FROM computers WHERE computer_id=?", (computer_id,)
                    ).fetchone()
                    if not current or current[0] != row["target_generation"]:
                        raise ValueError("rebind target changed")
                    generation = current[0] + 1
                    self.conn.execute(
                        "UPDATE computers SET credential_digest=?,enabled=0,generation=?,"
                        "updated_at=? WHERE computer_id=?",
                        (_digest(token), generation, now, computer_id),
                    )
                else:
                    generation = 1
                    self.conn.execute(
                        "INSERT INTO computers (computer_id,display_name,credential_digest,"
                        "enabled,generation,created_at,updated_at) VALUES (?,?,?,0,?,?,?)",
                        (computer_id, row["name"], _digest(token), generation, now, now),
                    )
                self.conn.execute(
                    "INSERT INTO pairing_bindings VALUES (?,?,?) ON CONFLICT(computer_id) DO "
                    "UPDATE SET owner_id=excluded.owner_id,generation=excluded.generation",
                    (computer_id, row["owner_id"], generation),
                )
                self.conn.execute(
                    "UPDATE pairing_requests SET state='issued',computer_id=?,credential=?,"
                    "generation=? WHERE request_id=?",
                    (computer_id, token, generation, request_id),
                )
                self._audit(request_id, "issued", "applicant")
                row = self._row(request_id)
            if row["state"] not in {"issued", "active"}:
                raise ValueError("pairing request unavailable")
            self._issuance_valid(row)
            result = {
                "status": row["state"],
                "computer_id": row["computer_id"],
                "credential": row["credential"],
            }
        self.store.computers._changed(row["computer_id"]) if row["state"] == "issued" else None
        register_secret(result["credential"])
        return result

    def _target_owner(self, row):
        binding = self.conn.execute(
            "SELECT owner_id FROM pairing_bindings WHERE computer_id=?", (row["target"],)
        ).fetchone()
        if binding is not None and binding[0] != row["owner_id"]:
            raise ValueError("rebind target belongs to another owner")

    def _issuance_valid(self, row):
        binding = self.conn.execute(
            "SELECT owner_id,generation FROM pairing_bindings WHERE computer_id=?",
            (row["computer_id"],),
        ).fetchone()
        if not binding or binding[0] != row["owner_id"] or binding[1] != row["generation"]:
            raise ValueError("computer access changed; pairing cannot resurrect it")
        current = self.conn.execute(
            "SELECT generation,credential_digest,enabled FROM computers WHERE computer_id=?",
            (row["computer_id"],),
        ).fetchone()
        if (
            not current
            or current[0] != row["generation"]
            or current[1] != _digest(row["credential"])
            or (row["state"] == "active" and not current[2])
            or not self.store.computers._pairing_allowed(row["computer_id"], row["generation"])
        ):
            raise ValueError("computer access changed; pairing cannot resurrect it")

    def ack(self, request_id, device_code, credential):
        with self._transaction():
            row = self._bound(request_id, device_code, allow_terminal=True)
            if row["state"] not in {"issued", "active"} or not hmac.compare_digest(
                row["credential"], credential
            ):
                raise ValueError("invalid credential acknowledgement")
            self._issuance_valid(row)
            if row["state"] != "active":
                self._audit(request_id, "active", "applicant")
            self.conn.execute(
                "UPDATE computers SET enabled=1 WHERE computer_id=?", (row["computer_id"],)
            )
            self.conn.execute(
                "UPDATE pairing_requests SET state='active' WHERE request_id=?", (request_id,)
            )
        return {"status": "active", "computer_id": row["computer_id"]}

    def activate_migration(self, actor, *, confirm=False):
        self.owner(actor)
        if actor != "local" or not confirm:
            raise ValueError("migration requires explicit local confirmation")
        if self.boundary.exists():
            return {"status": "already_active"}
        with self._transaction():
            epoch = secrets.token_hex(16)
            # Write-ahead external boundary intentionally fails closed on a crash before DB commit.
            if os.name == "nt":
                from acp_gateway.windows_acl import check_private_paths

                check_private_paths(
                    [self.boundary.parent],
                    directories=[self.boundary.parent],
                    purpose="Pairing migration boundary",
                )
            fd = os.open(self.boundary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps({"epoch": epoch, "rollback": "forward-only"}))
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "posix":
                parent = os.open(self.boundary.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            self.store.set_channel_state("pairing", "boundary", epoch)
            old = [
                r[0]
                for r in self.conn.execute(
                    "SELECT computer_id FROM computers WHERE computer_id NOT IN (SELECT "
                    "computer_id FROM pairing_bindings)"
                )
            ]
            self.conn.execute(
                "UPDATE computers SET enabled=0,generation=generation+1 WHERE computer_id "
                "NOT IN (SELECT computer_id FROM pairing_bindings)"
            )
        for computer_id in old:
            self.store.computers._changed(computer_id)
        return {"status": "active", "legacy_computers": old}

    def status(self, request_id, device_code):
        row = self._bound(request_id, device_code, allow_terminal=True)
        if row["state"] in {"issued", "active"}:
            self._issuance_valid(row)
        return {"status": row["state"]}

    def cancel(self, request_id, device_code):
        with self._transaction():
            row = self._bound(request_id, device_code, allow_terminal=True)
            if row["state"] in {"issued", "active"}:
                raise ValueError("already issued; revoke the computer locally")
            self.conn.execute(
                "UPDATE pairing_requests SET state='cancelled' WHERE request_id=?", (request_id,)
            )
        return {"status": "cancelled"}

    def _audit(self, request_id, event, actor):
        self.conn.execute(
            "INSERT INTO pairing_audit(request_id,event,actor,at) VALUES (?,?,?,?)",
            (request_id, event, actor, self.clock()),
        )
        self.conn.execute(
            "DELETE FROM pairing_audit WHERE id NOT IN (SELECT id FROM pairing_audit ORDER "
            "BY id DESC LIMIT 4096)"
        )

    def audit(self, actor):
        self.owner(actor)
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT request_id,event,actor,at FROM pairing_audit ORDER BY id"
            )
        ]
