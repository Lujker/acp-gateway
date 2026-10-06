"""Normalized agent errors. Channels turn them into user-facing text."""

from __future__ import annotations


class AgentError(Exception):
    """Base class; ``str(error)`` is safe to show to a user (no secrets)."""


class AgentUnavailable(AgentError):
    """The agent cannot be reached (connection refused, timeout, TLS failure)."""


class AuthenticationFailed(AgentError):
    """The agent rejected the shared secret."""


class TLSFingerprintMismatch(AgentError):
    """The agent presented a certificate that does not match the pin."""


class TransportDisconnected(AgentError):
    """The connection dropped while a request was in flight."""


class SessionNotFound(AgentError):
    """The agent does not know the session (or cannot load it)."""


class SessionBusy(AgentError):
    """The session already has a running turn."""


class ModeNotAvailable(AgentError):
    """The configured session mode is not offered by the agent."""


class PromptFailed(AgentError):
    """The agent returned an error for a prompt."""
