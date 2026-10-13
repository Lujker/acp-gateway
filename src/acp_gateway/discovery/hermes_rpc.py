"""Bounded Hermes diagnostic RPC connection. No reconnect or prompt replay.

This is a protocol probe, not an ACP adapter or a dispatcher-facing API.
Default access observes only. Mutations and automatic denial are opt-in and
restricted to explicitly selected diagnostic conversations, never Desktop's.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from urllib.parse import urlencode

from acp_gateway.discovery.hermes import HermesDiscoveryError, HermesEndpoint, verify_endpoint

MAX_FRAME = 1024 * 1024
MAX_PENDING = 32
_WIRE_LOG = logging.getLogger("acp_gateway.hermes.wire")
_WIRE_LOG.setLevel(logging.WARNING)
# Deliberately exclude configuration, secret management and plugin RPC methods.
METHODS = frozenset(
    {
        "ping",
        "gateway.capabilities",
        "client.capabilities",
        "session.active_list",
        "session.list",
        "session.create",
        "session.activate",
        "session.resume",
        "session.interrupt",
        "prompt.submit",
    }
)
MUTATIONS = frozenset({"session.create", "session.resume", "session.interrupt", "prompt.submit"})
TEST_PARAMS = {
    "prompt.submit": {"profile", "session_id", "text"},
    "session.interrupt": {"profile", "session_id"},
}


def stored_session_id(snapshot: dict) -> str | None:
    """Normalize Hermes create/activate/resume snapshots without guessing IDs."""
    values = [snapshot.get(key) for key in ("stored_session_id", "session_key")]
    present = [value for value in values if value is not None]
    if any(not isinstance(value, str) or not value for value in present):
        raise HermesDiscoveryError("invalid Hermes stored session identity")
    if len(set(present)) > 1:
        raise HermesDiscoveryError("conflicting Hermes stored session identities")
    return present[0] if present else None


class HermesRpcError(HermesDiscoveryError):
    def __init__(self, code: int):
        super().__init__(f"Hermes RPC failed (code {code})")
        self.code = code


class HermesRpcClient:
    def __init__(self, socket: Any, *, version: str, timeout: float, diagnostic: bool = False):
        self.version = version
        self.timeout = timeout
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)
        self.denied_requests: list[str] = []
        self.ignored_request_count = 0
        self.closed = asyncio.Event()
        self.failure: str | None = None
        self._socket = socket
        self._pending: dict[str, asyncio.Future] = {}
        self._sequence = 0
        self._send_lock = asyncio.Lock()
        self._diagnostic = diagnostic
        self._test_session: tuple[str, str] | None = None
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(
        cls, endpoint: HermesEndpoint, *, timeout: float = 5, diagnostic: bool = False
    ) -> HermesRpcClient:
        from websockets.asyncio.client import connect

        from acp_gateway.log import register_secret

        register_secret(endpoint.token)
        version = await verify_endpoint(endpoint, timeout=timeout)

        # Hermes loopback WS uses a query credential. It must never be logged,
        # passed through a proxy, or redirected to a different endpoint.
        class NoRedirect(connect):
            def process_redirect(self, exc):
                return exc

        try:
            socket = await NoRedirect(
                endpoint.base_url.replace("http://", "ws://", 1)
                + "/api/ws?"
                + urlencode({"token": endpoint.token}),
                proxy=None,
                max_size=MAX_FRAME,
                open_timeout=timeout,
                close_timeout=timeout,
                logger=_WIRE_LOG,
            )
        except Exception:
            # WebSocket exceptions can embed the credential-bearing URL.
            raise HermesDiscoveryError(
                "Hermes WebSocket connection failed", status="offline"
            ) from None
        return cls(socket, version=version, timeout=timeout, diagnostic=diagnostic)

    def select_test_session(self, session_id: str, *, profile: str) -> None:
        """Opt into diagnostic writes/denials for one dedicated test conversation.

        This is not an upstream ownership lease. Never use it for production
        shared-session control or to claim an arbitrary Desktop conversation.
        """
        if (
            not self._diagnostic
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(profile, str)
            or not profile
        ):
            raise HermesDiscoveryError("test session requires explicit diagnostic access")
        selected = (session_id, profile)
        if self._test_session is not None and self._test_session != selected:
            raise HermesDiscoveryError("diagnostic connection is already bound to a test session")
        self._test_session = selected

    def _authorize(self, method: str, params: dict) -> None:
        if method not in METHODS:
            raise HermesDiscoveryError("method is outside the Hermes probe allowlist")
        if method in MUTATIONS and not self._diagnostic:
            raise HermesDiscoveryError("Hermes observation does not permit mutation or cold resume")
        if (
            method == "client.capabilities"
            and not self._diagnostic
            and params.get("server_requests") is not False
        ):
            raise HermesDiscoveryError("Hermes observer cannot advertise approval handling")
        if method == "session.activate" and (
            params.get("omit_messages") is not True
            or set(params)
            - {
                "profile",
                "session_id",
                "omit_messages",
            }
        ):
            raise HermesDiscoveryError("Hermes observation requires a history-free activation")
        if method in TEST_PARAMS:
            if (
                not self._test_session
                or (params.get("session_id"), params.get("profile")) != self._test_session
            ):
                raise HermesDiscoveryError("Hermes diagnostic mutation is outside the test session")
            if set(params) - TEST_PARAMS[method]:
                raise HermesDiscoveryError("unsupported Hermes diagnostic mutation parameters")

    async def _send(self, frame: dict) -> None:
        data = json.dumps(frame, separators=(",", ":"))
        if len(data.encode()) > MAX_FRAME:
            raise HermesDiscoveryError("Hermes RPC request exceeds its size limit")
        async with self._send_lock:
            try:
                await asyncio.wait_for(self._socket.send(data), self.timeout)
            except Exception:
                raise HermesDiscoveryError("Hermes RPC delivery failed") from None

    async def call(self, method: str, params: dict | None = None) -> dict:
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise HermesDiscoveryError("Hermes RPC parameters must be an object")
        self._authorize(method, params)
        if self.closed.is_set() or len(self._pending) >= MAX_PENDING:
            raise HermesDiscoveryError("Hermes RPC connection is unavailable or busy")
        self._sequence += 1
        rid = f"acpgw-probe-{self._sequence}"
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, self.timeout)
        except TimeoutError:
            # A timed out mutation may have executed. The caller must not replay.
            raise HermesDiscoveryError("Hermes RPC timed out; outcome may be unknown") from None
        finally:
            self._pending.pop(rid, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # The reader may fail while send itself raises; retrieve any
                # exception so it cannot become an unobserved task warning.
                future.exception()

    async def _read(self) -> None:
        try:
            async for raw in self._socket:
                if not isinstance(raw, str):
                    raise ValueError
                frame = json.loads(raw)
                if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                    raise ValueError
                rid = frame.get("id")
                if rid is not None and type(rid) not in (str, int):
                    raise ValueError
                if isinstance(frame.get("method"), str):
                    if rid is not None:
                        await self._deny(frame)
                    else:
                        self.events.put_nowait(frame)
                elif (future := self._pending.get(rid)) is not None and not future.done():
                    if "error" in frame:
                        error = frame["error"]
                        if not isinstance(error, dict) or type(error.get("code")) is not int:
                            raise ValueError
                        future.set_exception(HermesRpcError(error["code"]))
                    elif isinstance(frame.get("result"), dict):
                        future.set_result(frame["result"])
                    else:
                        raise ValueError
        except Exception:
            # Protocol, queue overflow and network errors fence this connection.
            self.failure = "connection_or_protocol_failure"
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(HermesDiscoveryError("Hermes RPC connection closed"))
            with contextlib.suppress(Exception):
                await self._socket.close()

    async def _deny(self, frame: dict) -> None:
        params = frame.get("params")
        if not isinstance(params, dict):
            raise ValueError
        if (
            not self._test_session
            or (params.get("session_id"), params.get("profile", self._test_session[1]))
            != self._test_session
        ):
            # An error response also settles Hermes' shared pending request.
            # Bystanders must leave every decision to the owning Desktop peer.
            self.ignored_request_count += 1
            return
        method = frame["method"]
        if len(self.denied_requests) < 128:
            self.denied_requests.append(method)
        if method == "approval":
            response = {"result": {"choice": "deny"}}
        else:
            response = {"error": {"code": -32601, "message": "Unsupported probe request"}}
        await self._send({"jsonrpc": "2.0", "id": frame["id"], **response})

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader
        # Cancellation before the reader starts never enters its finally block.
        self.closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HermesDiscoveryError("Hermes RPC connection closed"))
        with contextlib.suppress(Exception):
            await self._socket.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
