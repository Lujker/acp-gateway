"""The channel contract.

A channel is a way for people (Telegram, CLI) or other agents (Hermes over
MCP) to reach the gateway. A new channel implements :class:`Channel` and does
not touch the core:

- ``name`` is unique and becomes ``Conversation.channel`` for every
  conversation the channel opens; the conversation ``key`` is the channel's own
  identifier (a chat id, a Hermes ``thread``, ``default``).
- The core calls :meth:`Channel.start` once when it starts and
  :meth:`Channel.stop` when it shuts down. In ``start`` the channel keeps the
  core and talks to it directly: ``submit``/``ask``/``wait``/``cancel`` for
  turns, ``new_session``/``sessions``/``switch_session`` for sessions.
- Results arrive either from ``wait``/``ask`` or as events on
  ``core.bus`` (subscribe with ``for_channel(self.name)``). By default a channel
  forwards only the request, a short "working" status (``JobStarted``) and the
  final answer (``JobFinished``); ``JobProgress`` — tool calls, plan, reasoning —
  is for detailed clients such as the local API's SSE stream.
- ``str()`` of an ``AgentError`` or a ``GatewayError`` is safe to show to a
  user; anything else is an internal error and is shown generically.
- Human channels set ``can_approve=True`` and report live human availability
  through ``connected``. They subscribe with ``for_approver(name)`` or list
  ``core.pending_approvals(name)`` and call ``core.resolve_approval`` with an
  authenticated human identity. The adapter must authenticate that identity;
  the core checks channel eligibility, request ownership, option and expiry.
  MCP/LLM channels never expose approval operations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from acp_gateway.core.gateway import GatewayCore


class Channel(ABC):
    name: str
    can_approve: bool = False

    @abstractmethod
    async def start(self, core: GatewayCore) -> None:
        """Begin serving; called once by the core."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop serving and release resources; must not raise for an unstarted channel."""

    @property
    def connected(self) -> bool:
        """Whether a human or a client is reachable through this channel right now."""
        return True
