"""Session-scoped Hermes model control, without exposing configuration RPCs.

Catalogue values are observations, not evidence of which provider served a
request. Deferred changes remain pending until a subsequent owned turn ends
and a fresh catalogue reports the requested pair.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

from acp_gateway.agents.model_control import identifier
from acp_gateway.discovery.hermes import HermesDiscoveryError


class HermesModelRefused(HermesDiscoveryError):
    """The adapter rejected a model mutation before calling the upstream setter."""


def model_identifier(value: str) -> str:
    try:
        return identifier(value)
    except ValueError:
        raise HermesDiscoveryError("invalid Hermes provider or model identifier") from None


@dataclass
class HermesModelControl:
    busy: bool = False
    authorization: tuple[str, dict] | None = None
    pending: tuple[str, str] | None = None
    turn_completed: bool = False
    confirmation: tuple[str, str, str, float] | None = None
    observed: tuple[str, str] | None = None
    confirmation_observed: tuple[str, str] | None = None
    # Safe catalogue projection only; upstream credentials/endpoints are discarded.
    providers: list[dict] = field(default_factory=list)

    def invalidate_confirmation(self):
        self.confirmation = None
        self.confirmation_observed = None

    def parse_catalogue(self, payload: dict) -> dict:
        provider, model = payload.get("provider"), payload.get("model")
        observed = (model_identifier(provider), model_identifier(model))
        rows = payload.get("providers")
        if not isinstance(rows, list) or len(rows) > 128:
            raise HermesDiscoveryError("invalid Hermes model catalogue")
        providers = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("models"), list):
                raise HermesDiscoveryError("invalid Hermes model catalogue")
            slug = model_identifier(row.get("slug"))
            if len(row["models"]) > 4096:
                raise HermesDiscoveryError("Hermes model catalogue exceeds its limit")
            models = list(dict.fromkeys(model_identifier(m) for m in row["models"]))
            providers.append({"provider": slug, "models": models})
        if self.observed is not None and self.observed != observed:
            self.invalidate_confirmation()
        self.observed, self.providers = observed, providers
        if self.pending == observed and self.turn_completed:
            self.pending = None
            self.turn_completed = False
        return {
            "provider": provider,
            "model": model,
            "providers": providers,
            "observation_source": "model.options",
            "pending": (
                {"provider": self.pending[0], "model": self.pending[1]} if self.pending else None
            ),
        }

    def consume_confirmation(self, provider: str, model: str, token: str | None) -> bool:
        if token is None:
            self.invalidate_confirmation()
            return False
        pending = self.confirmation
        if (
            pending is None
            or pending[:3] != (provider, model, token)
            or time.monotonic() > pending[3]
            or self.confirmation_observed != self.observed
        ):
            raise HermesModelRefused("Hermes model confirmation is stale or belongs elsewhere")
        self.invalidate_confirmation()
        return True

    def accept_response(self, payload: dict, provider: str, model: str) -> dict:
        if payload.get("key") != "model" or payload.get("scope") != "session":
            raise HermesDiscoveryError("Hermes did not confirm session-scoped model selection")
        confirm, deferred = payload.get("confirm_required", False), payload.get("deferred", False)
        if type(confirm) is not bool or type(deferred) is not bool or (confirm and deferred):
            raise HermesDiscoveryError("invalid Hermes model acknowledgement")
        if confirm:
            message = payload.get("confirm_message") or payload.get("warning")
            if (
                not isinstance(message, str)
                or not message.strip()
                or len(message) > 4096
                or any(ord(c) < 32 and c not in "\n\t" for c in message)
            ):
                raise HermesDiscoveryError("invalid Hermes model confirmation message")
            token = uuid4().hex
            self.confirmation = (provider, model, token, time.monotonic() + 300)
            self.confirmation_observed = self.observed
            return {
                "status": "confirmation_required",
                "confirmation_token": token,
                "message": message,
            }
        # Hermes may canonicalize model/provider names. Do not call an acknowledgement
        # proof of the runtime pair; the caller must fetch a fresh observation.
        resolved = model_identifier(payload.get("value"))
        self.pending = (provider, resolved)
        # An immediate acknowledgement still requires a fresh observation.
        # A deferred one also requires a subsequent owned turn to complete.
        self.turn_completed = not deferred
        result = {"status": "deferred" if deferred else "accepted"}
        if resolved != model:
            result["resolved_request"] = {"provider": provider, "model": resolved}
        return result
