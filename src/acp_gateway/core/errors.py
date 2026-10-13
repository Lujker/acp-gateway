"""Gateway-level errors; like agent errors, ``str(error)`` is safe to show to a user."""

from __future__ import annotations


class GatewayError(Exception):
    """Base class of errors raised by the core itself (not by an agent)."""


class RateLimited(GatewayError):
    """New work exceeded its channel's aggregate admission budget."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Too many work requests; retry after {retry_after} seconds.")


class MaintenanceBlocked(GatewayError):
    """Maintenance cannot reserve an idle core, or new work is currently paused."""


class SelectionChanged(GatewayError):
    """A delayed action no longer matches the persistent active selection."""


class UnknownAgent(GatewayError):
    """No agent profile with this alias."""


class UnknownSession(GatewayError):
    """The conversation has no session with this id."""


class JobNotFound(GatewayError):
    """No job with this id (never existed or already pruned)."""


class PolicyDenied(GatewayError):
    """An operation is disabled or exceeds a configured limit."""


class ApprovalNotFound(GatewayError):
    """An approval does not exist or has already been settled."""
