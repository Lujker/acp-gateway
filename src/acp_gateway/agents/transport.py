"""ACP message transport over WebSocket with an auth header and pinned TLS.

The SDK's ``acp.ws.create_websocket_stream`` cannot take an SSL context
(acp 0.12), hence this small implementation of the same ``Transport``
protocol. Streamable HTTP is not used: it cannot continue a session after
``session/load`` (see docs/architecture.md, section 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from acp_gateway.agents.errors import (
    AgentUnavailable,
    AuthenticationFailed,
    TLSFingerprintMismatch,
)
from acp_gateway.agents.tls import TlsPin, fingerprint_of

MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class PinnedWebSocketTransport:
    """Moves JSON-RPC messages; ``closed`` is set once the socket is gone."""

    def __init__(self, connection: Any) -> None:
        self._ws = connection
        self.closed = asyncio.Event()

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str],
        pin: TlsPin | None,
        open_timeout: float = 15,
    ) -> PinnedWebSocketTransport:
        try:
            connection = await ws_connect(
                url,
                additional_headers=headers,
                ssl=pin.context if pin else None,
                max_size=MAX_MESSAGE_BYTES,
                open_timeout=open_timeout,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise AuthenticationFailed("the agent rejected the secret") from exc
            raise AgentUnavailable(f"the agent refused the connection (HTTP {status})") from exc
        except (OSError, TimeoutError) as exc:
            raise AgentUnavailable(
                f"cannot connect to the agent: {exc or type(exc).__name__}"
            ) from exc

        if pin is not None:
            der = connection.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if fingerprint_of(der) != pin.fingerprint:  # defence in depth after the probe
                await connection.close()
                raise TLSFingerprintMismatch("TLS fingerprint changed between probe and connect")
        return cls(connection)

    async def send(self, message: dict[str, Any]) -> None:
        try:
            await self._ws.send(json.dumps(message, separators=(",", ":")))
        except ConnectionClosed as exc:
            self.closed.set()
            raise ConnectionError("WebSocket closed") from exc

    async def receive(self) -> dict[str, Any] | None:
        while True:
            try:
                frame = await self._ws.recv()
            except ConnectionClosed:
                self.closed.set()
                return None
            if isinstance(frame, bytes):
                continue
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(frame)

    async def close(self) -> None:
        self.closed.set()
        with contextlib.suppress(Exception):
            await self._ws.close()
