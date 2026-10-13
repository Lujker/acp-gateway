"""Durable latest model intent and observation per owned dialogue.

Only projected identifiers are stored. Confirmation authority, credentials and
host error text never enter this table. Unknown outcomes require an explicit
future reconciliation contract; a matching catalogue alone cannot clear them.
"""

import json

from acp_gateway.agents.model_control import identifier
from acp_gateway.storage.records import utcnow


class ModelStateStore:
    def __init__(self, conn):
        self._conn = conn

    def state(self, session_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM model_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["deferred"] = bool(result["deferred"])
        result["turn_completed"] = bool(result["turn_completed"])
        result["operation_id"] = self._value("model_operation_id", session_id)
        result["resolved_request"] = self._value("model_resolved_request", session_id)
        return result

    def _value(self, namespace, session_id):
        row = self._conn.execute(
            "SELECT value FROM channel_state WHERE namespace = ? AND key = ?",
            (namespace, str(session_id)),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _set_value(self, namespace, session_id, value):
        self._conn.execute(
            "INSERT INTO channel_state (namespace, key, value) VALUES (?, ?, ?)"
            " ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
            (namespace, str(session_id), json.dumps(value)),
        )

    def unresolved_count(self) -> int:
        return self._conn.execute(
            "SELECT count(*) FROM model_state WHERE state IN ('intent', 'pending', 'unknown')"
        ).fetchone()[0]

    def recover_intents(self) -> int:
        cursor = self._conn.execute(
            "UPDATE model_state SET state = 'unknown', updated_at = ? WHERE state = 'intent'",
            (utcnow().isoformat(),),
        )
        return cursor.rowcount

    def begin(
        self,
        session_id: int,
        provider: str,
        model: str,
        revision: int,
        *,
        operation_id: str | None = None,
    ) -> None:
        provider, model = identifier(provider), identifier(model)
        self._conn.execute("BEGIN")
        with self._conn:
            self._conn.execute(
                "INSERT INTO model_state (session_id, state, requested_provider, requested_model,"
                " selection_revision, updated_at) VALUES (?, 'intent', ?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET state = 'intent',"
                " requested_provider = excluded.requested_provider,"
                " requested_model = excluded.requested_model,"
                " selection_revision = excluded.selection_revision, deferred = 0,"
                " turn_completed = 0, updated_at = excluded.updated_at",
                (session_id, provider, model, revision, utcnow().isoformat()),
            )

            self._set_value("model_operation_id", session_id, operation_id)
            self._set_value("model_resolved_request", session_id, None)

    def settle(
        self, session_id: int, state: str, *, deferred: bool = False, resolved_request=None
    ) -> None:
        if state not in {"pending", "unknown", "refused", "confirmation_required"}:
            raise ValueError("invalid model mutation outcome")
        if resolved_request is not None:
            clean = {
                "provider": identifier(resolved_request["provider"]),
                "model": identifier(resolved_request["model"]),
            }
            self._set_value("model_resolved_request", session_id, clean)
        self._conn.execute(
            "UPDATE model_state SET state = ?, deferred = ?, updated_at = ? WHERE session_id = ?",
            (state, int(deferred), utcnow().isoformat(), session_id),
        )

    def turn_finished(self, session_id: int) -> None:
        self._conn.execute(
            "UPDATE model_state SET turn_completed = 1, updated_at = ?"
            " WHERE session_id = ? AND state = 'pending'",
            (utcnow().isoformat(), session_id),
        )

    def observe(self, session_id: int, result: dict) -> None:
        provider, model = identifier(result["provider"]), identifier(result["model"])
        if result["observation_source"] != "model.options":
            raise ValueError("invalid model observation source")
        previous = self.state(session_id)
        state = previous["state"] if previous else "observed"
        resolved = previous["resolved_request"] if previous else None
        requested = (
            (resolved["provider"], resolved["model"])
            if resolved
            else (previous["requested_provider"], previous["requested_model"])
            if previous
            else None
        )
        if (
            previous
            and state == "pending"
            and result["pending"] is None
            and (provider, model) == requested
            and (not previous["deferred"] or previous["turn_completed"])
        ):
            state = "observed"
        self._conn.execute(
            "INSERT INTO model_state (session_id, state, observed_provider, observed_model,"
            " observation_source, updated_at) VALUES (?, ?, ?, ?, 'model.options', ?)"
            " ON CONFLICT(session_id) DO UPDATE SET state = excluded.state,"
            " observed_provider = excluded.observed_provider,"
            " observed_model = excluded.observed_model,"
            " observation_source = excluded.observation_source, updated_at = excluded.updated_at",
            (session_id, state, provider, model, utcnow().isoformat()),
        )
