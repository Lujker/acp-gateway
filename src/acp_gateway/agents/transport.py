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
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from acp_gateway.agents.errors import (
    AgentUnavailable,
    AuthenticationFailed,
    TLSFingerprintMismatch,
)
from acp_gateway.agents.tls import TlsPin, fingerprint_of
from acp_gateway.log import get_logger

MAX_MESSAGE_BYTES = 64 * 1024 * 1024

_log = get_logger(__name__)


class AgentTransport(Protocol):
    """One ACP stream owned by AgentClient.

    Implementations signal EOF or delivery failure through ``closed`` and
    return None from receive on EOF. Close must be idempotent. A relay stream
    closes independently of its shared computer connection.
    """

    closed: asyncio.Event

    async def send(self, message: dict[str, Any]) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


# Each call opens a fresh stream; credentials and routing belong to the factory.
TransportFactory = Callable[[], Awaitable[AgentTransport]]


def _acceptable(message: Any) -> bool:
    """Whether the SDK receive loop can process ``message`` without dying.

    The loop stops on shapes it does not expect (a batch array, an unhashable
    id, a non-object error) while the socket stays open, which would leave a
    connection that looks alive but answers nothing.
    """
    if not isinstance(message, dict):
        return False
    if "id" in message and (
        isinstance(message["id"], bool) or not isinstance(message["id"], int | str | None)
    ):
        return False
    if message.get("method") is not None:
        return isinstance(message["method"], str)
    return isinstance(message.get("error"), dict | None)


class _NoRedirectConnect(ws_connect):
    """Credentials and the TLS pin belong only to the configured endpoint."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


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
            connection = await _NoRedirectConnect(
                url,
                additional_headers=headers,
                ssl=pin.context if pin else None,
                max_size=MAX_MESSAGE_BYTES,
                open_timeout=open_timeout,
                # The secret and the TLS pin are for a direct connection to the
                # configured endpoint; environment proxies must not see them.
                proxy=None,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise AuthenticationFailed("the agent rejected the secret") from exc
            raise AgentUnavailable(f"the agent refused the connection (HTTP {status})") from exc
        except InvalidHandshake as exc:
            raise AgentUnavailable(
                f"WebSocket handshake with the agent failed ({type(exc).__name__})"
            ) from exc
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
            try:
                message = json.loads(frame)
            except (ValueError, RecursionError):
                _log.warning("agent sent a frame that is not JSON; ignored")
                continue
            if _acceptable(message):
                return message
            _log.warning("agent sent an invalid JSON-RPC message; ignored")

    async def close(self) -> None:
        self.closed.set()
        with contextlib.suppress(Exception):
            await self._ws.close()
