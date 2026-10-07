"""Human availability is the lifetime of an authenticated approvals stream."""

from __future__ import annotations

import secrets

from acp_gateway.channels.base import Channel
from acp_gateway.core.errors import PolicyDenied


class CliChannel(Channel):
    name = "cli"
    can_approve = True

    def __init__(self) -> None:
        self._started = False
        self._leases: dict[str, str] = {}

    async def start(self, core) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self._leases.clear()

    @property
    def connected(self) -> bool:
        return self._started and bool(self._leases)

    def attach(self, actor: str) -> str:
        if not self._started:
            raise PolicyDenied("the CLI channel is not running")
        lease = secrets.token_urlsafe(24)
        self._leases[lease] = actor
        return lease

    def detach(self, lease: str) -> None:
        self._leases.pop(lease, None)

    def actor(self, lease: str) -> str:
        if not self._started or lease not in self._leases:
            raise PolicyDenied("a live approvals connection is required")
        return self._leases[lease]
