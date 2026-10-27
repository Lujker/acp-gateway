"""Persistent result delivery state, containing references rather than answer copies."""

import sqlite3
import time


class DeliveryStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def get(self, namespace: str, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM outbound_deliveries WHERE namespace = ? AND job_id = ?",
            (namespace, job_id),
        ).fetchone()
        return dict(row) if row else None

    def recover(self, namespace: str) -> None:
        # Telegram has no send idempotency key. A vanished sender may already
        # have delivered the chunk, so it must never be replayed automatically.
        self._conn.execute(
            "UPDATE outbound_deliveries SET state = 'unknown' "
            "WHERE namespace = ? AND state = 'sending'",
            (namespace,),
        )

    def due(self, namespace: str) -> dict | None:
        row = self._conn.execute(
            "SELECT d.*, s.conversation_key FROM outbound_deliveries d "
            "JOIN jobs j ON j.id = d.job_id JOIN sessions s ON s.id = j.session_id "
            "WHERE d.namespace = ? AND d.state = 'pending' AND d.retry_at <= ? "
            "ORDER BY d.retry_at, j.created_at, j.id LIMIT 1",
            (namespace, time.time()),
        ).fetchone()
        return dict(row) if row else None

    def claim(self, namespace: str, job_id: str) -> bool:
        return (
            self._conn.execute(
                "UPDATE outbound_deliveries SET state = 'sending' "
                "WHERE namespace = ? AND job_id = ? AND state = 'pending'",
                (namespace, job_id),
            ).rowcount
            == 1
        )

    def bind_content(self, namespace: str, job_id: str, digest: str) -> bool:
        row = self.get(namespace, job_id)
        if row is None or row["state"] != "sending":
            return False
        if row["content_hash"] not in (None, digest):
            self.settle(namespace, job_id, "unknown")
            return False
        self._conn.execute(
            "UPDATE outbound_deliveries SET content_hash = ? "
            "WHERE namespace = ? AND job_id = ? AND state = 'sending'",
            (digest, namespace, job_id),
        )
        return True

    def pin_presentation(self, namespace: str, job_id: str, footer: str) -> str:
        self._conn.execute(
            "UPDATE outbound_deliveries SET presentation = COALESCE(presentation, ?) "
            "WHERE namespace = ? AND job_id = ? AND state = 'sending'",
            (footer, namespace, job_id),
        )
        row = self.get(namespace, job_id)
        if row is None or row["state"] != "sending" or row["presentation"] is None:
            raise ValueError("delivery is not claimed")
        return row["presentation"]

    def acknowledge(self, namespace: str, job_id: str, *, last: bool) -> None:
        self._conn.execute(
            "UPDATE outbound_deliveries SET next_chunk = next_chunk + 1, state = ?, retry_at = 0 "
            "WHERE namespace = ? AND job_id = ? AND state = 'sending'",
            ("delivered" if last else "pending", namespace, job_id),
        )

    def settle(self, namespace: str, job_id: str, state: str, *, retry_after: float = 0) -> None:
        if state not in {"pending", "failed", "unknown"}:
            raise ValueError("invalid delivery outcome")
        self._conn.execute(
            "UPDATE outbound_deliveries SET state = ?, retry_at = ? "
            "WHERE namespace = ? AND job_id = ? AND state = 'sending'",
            (state, time.time() + max(1, retry_after), namespace, job_id),
        )
