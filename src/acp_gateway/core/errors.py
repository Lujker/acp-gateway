"""Gateway-level errors; like agent errors, ``str(error)`` is safe to show to a user."""

from __future__ import annotations


class GatewayError(Exception):
    """Base class of errors raised by the core itself (not by an agent)."""


class UnknownAgent(GatewayError):
    """No agent profile with this alias."""


class UnknownSession(GatewayError):
    """The conversation has no session with this id."""


class JobNotFound(GatewayError):
    """No job with this id (never existed or already pruned)."""
