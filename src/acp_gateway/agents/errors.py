"""Normalized agent errors. Channels turn them into user-facing text."""

from __future__ import annotations


class AgentError(Exception):
    """Base class; ``str(error)`` is safe to show to a user (no secrets)."""


class AgentUnavailable(AgentError):
    """The agent cannot be reached (connection refused, timeout, TLS failure)."""


class ManagedCapacity(AgentUnavailable):
    """An owned managed process limit prevents another launch."""


class AuthenticationFailed(AgentError):
    """The agent rejected the shared secret."""


class TLSFingerprintMismatch(AgentError):
    """The agent presented a certificate that does not match the pin."""


class TransportDisconnected(AgentError):
    """The connection dropped while a request was in flight."""


class SessionNotFound(AgentError):
    """The agent does not know the session (or cannot load it)."""


class SessionInterrupted(SessionNotFound):
    """The dialogue's last turn was interrupted with an unknown outcome.

    It can be resumed explicitly; the interrupted prompt is never replayed.
    """


class SessionFenced(SessionNotFound):
    """The computer never confirmed that earlier work stopped; the binding is fenced.

    The owner may release it explicitly after checking; nothing is replayed.
    """


class SessionBusy(AgentError):
    """The session already has a running turn."""


class ModeNotAvailable(AgentError):
    """The configured session mode is not offered by the agent."""


class PromptFailed(AgentError):
    """The agent returned an error for a prompt."""


class TurnFailed(PromptFailed):
    """The agent ended the turn with an error; the dialogue stays usable."""


class ModelRefused(AgentError):
    """A known refusal before the model setter executed; safe to refresh, never auto-retry."""
