"""Connections to ACP agents."""

from acp_gateway.agents.client import (
    AgentClient,
    AgentReply,
    PermissionHandler,
    PermissionOption,
    PermissionRequest,
    reject_all,
)
from acp_gateway.agents.errors import (
    AgentError,
    AgentUnavailable,
    AuthenticationFailed,
    ModeNotAvailable,
    PromptFailed,
    SessionBusy,
    SessionNotFound,
    TLSFingerprintMismatch,
    TransportDisconnected,
)

__all__ = [
    "AgentClient",
    "AgentError",
    "AgentReply",
    "AgentUnavailable",
    "AuthenticationFailed",
    "ModeNotAvailable",
    "PermissionHandler",
    "PermissionOption",
    "PermissionRequest",
    "PromptFailed",
    "SessionBusy",
    "SessionNotFound",
    "TLSFingerprintMismatch",
    "TransportDisconnected",
    "reject_all",
]
