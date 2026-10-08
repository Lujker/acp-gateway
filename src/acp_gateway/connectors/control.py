"""Computer ingress and an outgoing registration client over WS or WSS.

The connection advertises agents, monitors identity/liveness, and multiplexes
guarded ACP streams. The owner API remains a separate loopback listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import ssl
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from websockets.asyncio.server import serve
from websockets.datastructures import MultipleValuesError
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from acp_gateway.agents.errors import AgentError, AgentUnavailable, TLSFingerprintMismatch
from acp_gateway.agents.tls import fingerprint_of, pin_certificate
from acp_gateway.agents.transport import _NoRedirectConnect
from acp_gateway.connectors.protocol import (
    MAX_DATA_BYTES,
    AgentManifest,
    Hello,
    Ping,
    ProtocolError,
    Welcome,
    decode_frame,
    encode_frame,
)
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.log import register_secret

# Library DEBUG traces include handshake headers. Never enable wire tracing.
_WIRE_LOG = logging.getLogger("acp_gateway.connectors.wire")
_WIRE_LOG.setLevel(logging.WARNING)


class ConnectorUnavailable(ValueError):
    """Transient computer connection failure; safe to retry registration."""


class ConnectorAccessError(ValueError):
    """Credentials, TLS or protocol need correction; do not retry automatically."""


@dataclass
class Registration:
    computer_id: str
    connection_id: UUID
    generation: int
    agents: tuple[AgentManifest, ...]
    connection: object
    relay: RelayPeer | None = None


class ControlDispatcher:
    def __init__(
        self,
        registry,
        *,
        heartbeat_seconds: int = 20,
        connect_path: str = "/connect",
        max_connections: int = 64,
    ):
        if not 5 <= heartbeat_seconds <= 300:
            raise ValueError("heartbeat must be between 5 and 300 seconds")
        self.registry = registry
        self.connect_path = validate_connect_path(connect_path)
        self.heartbeat_seconds = heartbeat_seconds
        if not 1 <= max_connections <= 1024:
            raise ValueError("ingress connection limit must be between 1 and 1024")
        self.max_connections = max_connections
        self.active: dict[str, Registration] = {}
        self._closing: set[asyncio.Task] = set()
        self.registry.subscribe(self._access_changed)

    def _access_changed(self, computer_id):
        registration = self.active.get(computer_id)
        if registration is not None and not registration.relay.closed.is_set():
            registration.relay.invalidate()
            task = asyncio.create_task(
                registration.connection.close(code=1008, reason="computer access changed")
            )
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def open_agent(self, computer_id: str, alias: str):
        registration = self.active.get(computer_id)
        if registration is None or registration.relay is None:
            raise AgentUnavailable("computer is offline")
        try:
            valid = self.registry.grant_valid(computer_id, registration.generation)
        except sqlite3.Error:
            registration.relay.invalidate()
            await registration.connection.close(code=1011, reason="access check unavailable")
            raise AgentUnavailable("computer access check is unavailable") from None
        if not valid:
            registration.relay.invalidate()
            await registration.connection.close(code=1008, reason="computer access changed")
            raise AgentUnavailable("computer access changed")
        return await registration.relay.open(alias)

    def authenticate(self, connection, request):
        if len(connection.server.handler_tasks) > self.max_connections:
            return connection.respond(503, "Computer ingress is full\n")
        if request.path != self.connect_path:
            return connection.respond(404, "Not found\n")
        try:
            identity = request.headers.get("X-ACP-Computer", "")
            authorization = request.headers.get("Authorization", "")
        except MultipleValuesError:
            return connection.respond(401, "Unauthorized\n")
        if not authorization.startswith("Bearer "):
            return connection.respond(401, "Unauthorized\n")
        try:
            generation = self.registry.authorize(identity, authorization[7:])
        except sqlite3.Error:
            return connection.respond(503, "Access check unavailable\n")
        if generation is None:
            return connection.respond(401, "Unauthorized\n")
        connection.computer_grant = (identity, generation)
        return None

    async def _watch_grant(self, registration):
        while True:
            await asyncio.sleep(1)
            try:
                valid = self.registry.grant_valid(registration.computer_id, registration.generation)
            except sqlite3.Error:
                if registration.relay is not None:
                    registration.relay.invalidate()
                await registration.connection.close(code=1011, reason="access check unavailable")
                return
            if not valid:
                if registration.relay is not None:
                    registration.relay.invalidate()
                await registration.connection.close(code=1008, reason="computer access changed")
                return

    async def handle(self, connection):
        registration = None
        watcher = None
        try:
            identity, generation = connection.computer_grant
            frame = decode_frame(await asyncio.wait_for(connection.recv(), timeout=5))
            if not isinstance(frame, Hello) or frame.computer_id != identity:
                raise ProtocolError("invalid registration")
            if not self.registry.grant_valid(identity, generation):
                raise ProtocolError("computer access changed")
            previous = self.active.get(identity)
            registration = Registration(
                identity, uuid4(), generation, tuple(frame.agents), connection
            )
            registration.relay = RelayPeer(
                connection, registration.connection_id, [agent.alias for agent in frame.agents]
            )
            self.active[identity] = registration
            if previous is not None:
                if previous.relay is not None:
                    previous.relay.invalidate()
                await previous.connection.close(code=1008, reason="computer connection replaced")
            if self.active.get(identity) is not registration:
                raise ProtocolError("computer connection replaced")
            if not self.registry.grant_valid(identity, generation):
                raise ProtocolError("computer access changed")
            watcher = asyncio.create_task(self._watch_grant(registration))
            await connection.send(
                encode_frame(
                    Welcome(
                        connection_id=registration.connection_id,
                        heartbeat_seconds=self.heartbeat_seconds,
                    )
                )
            )
            await connection.send(encode_frame(Ping(sequence=0)))
            await registration.relay.run()
        except (ProtocolError, TimeoutError):
            await connection.close(code=1008, reason="control protocol rejected")
        except sqlite3.Error:
            await connection.close(code=1011, reason="access check unavailable")
        except (ConnectionClosed, ConnectionError):
            pass
        finally:
            if registration is not None and registration.relay is not None:
                registration.relay.invalidate()
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            if (
                registration is not None
                and self.active.get(registration.computer_id) is registration
            ):
                del self.active[registration.computer_id]

    def listen(self, host: str, port: int, *, tls: ssl.SSLContext | None = None):
        if tls is not None and tls.protocol != ssl.PROTOCOL_TLS_SERVER:
            raise ValueError("dispatcher requires a server TLS context")
        return serve(
            self.handle,
            host,
            port,
            ssl=tls,
            process_request=self.authenticate,
            max_size=MAX_DATA_BYTES,
            max_queue=4,
            compression=None,
            ping_interval=self.heartbeat_seconds,
            ping_timeout=self.heartbeat_seconds,
            open_timeout=5,
            close_timeout=2,
            origins=[None],
            logger=_WIRE_LOG,
        )


def read_credential(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256:
            raise ValueError("credential must be a small regular file")
        if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise ValueError(
                "credential file must belong to this user and have private permissions"
            )
        # Decode errors would echo credential bytes; reject them uniformly.
        credential = stream.read(257).decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"acpc_[A-Za-z0-9_-]{43}", credential):
        raise ValueError("invalid computer credential file")
    register_secret(credential)
    return credential


def validate_connect_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or len(path) > 1024
        or not re.fullmatch(r"/[A-Za-z0-9/_~.-]*", path)
        or any(part in {".", ".."} for part in path.split("/"))
        or "//" in path
    ):
        raise ValueError("connection path must be an absolute URL path without query or traversal")
    return path


def validate_url(url: str):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"ws", "wss"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(c.isspace() for c in url)
            or (parts.port is not None and not 1 <= parts.port <= 65535)
        ):
            raise ValueError
        validate_connect_path(parts.path or "/")
    except ValueError:
        raise ValueError(
            "dispatcher URL must be ws:// or wss://host[:port][/path] without credentials or query"
        ) from None
    return parts


def normalize_pin(value: str) -> str:
    hex_digits = re.sub(r"[\s:]", "", value).upper()
    if not re.fullmatch(r"[0-9A-F]{64}", hex_digits):
        raise ValueError("dispatcher TLS pin must be a SHA-256 fingerprint")
    return ":".join(hex_digits[i : i + 2] for i in range(0, 64, 2))


async def connect_control(
    url: str, *, credential: str, hello: Hello, fingerprint: str | None = None
):
    parts = validate_url(url)
    if fingerprint is not None and parts.scheme != "wss":
        raise ValueError("a TLS fingerprint requires a wss:// dispatcher URL")
    fingerprint = normalize_pin(fingerprint) if fingerprint is not None else None
    pin = None
    tls = None
    try:
        if parts.scheme == "wss":
            if fingerprint is not None:
                pin = await pin_certificate(
                    parts.hostname,
                    parts.port or 443,
                    configured=fingerprint,
                    pin_file=Path("unused-pin"),
                )
                tls = pin.context
            else:
                tls = ssl.create_default_context()
        connection = await _NoRedirectConnect(
            url,
            additional_headers={
                "Authorization": f"Bearer {credential}",
                "X-ACP-Computer": hello.computer_id,
            },
            ssl=tls,
            proxy=None,
            max_size=MAX_DATA_BYTES,
            max_queue=4,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=2,
            open_timeout=10,
            logger=_WIRE_LOG,
        )
    except InvalidStatus as exc:
        error = ConnectorAccessError if exc.response.status_code < 500 else ConnectorUnavailable
        raise error("cannot connect to dispatcher: verify endpoint and computer access") from None
    except (TLSFingerprintMismatch, ssl.SSLCertVerificationError):
        raise ConnectorAccessError(
            "cannot connect to dispatcher: verify TLS pin or certificate trust"
        ) from None
    except (AgentError, OSError, TimeoutError, InvalidHandshake):
        raise ConnectorUnavailable(
            "cannot connect to dispatcher: verify TLS pin, reachability and computer access"
            if parts.scheme == "wss"
            else "cannot connect to dispatcher: verify reachability and computer access"
        ) from None
    try:
        if pin is not None:
            der = connection.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if fingerprint_of(der) != pin.fingerprint:
                raise ConnectorAccessError(
                    "dispatcher TLS certificate changed between probe and connect"
                )
        await connection.send(encode_frame(hello))
        welcome = decode_frame(await asyncio.wait_for(connection.recv(), timeout=5))
        if not isinstance(welcome, Welcome):
            raise ProtocolError("dispatcher did not welcome this computer")
        return connection, welcome
    except BaseException as exc:
        close_code = connection.close_code
        await connection.close()
        if isinstance(exc, TimeoutError) or (
            isinstance(exc, ConnectionClosed) and close_code != 1008
        ):
            raise ConnectorUnavailable("dispatcher registration interrupted") from None
        if isinstance(exc, (ConnectionClosed, ProtocolError)):
            raise ConnectorAccessError("dispatcher rejected computer registration") from None
        raise


async def run_connector(
    url: str,
    *,
    credential: str,
    hello: Hello,
    fingerprint: str | None = None,
    local_factory=None,
):
    delays = (1, 2, 5, 10, 30)
    attempt = 0
    while True:
        try:
            await _run_connection(
                url,
                credential=credential,
                hello=hello,
                fingerprint=fingerprint,
                local_factory=local_factory,
            )
        except ConnectorUnavailable:
            await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
            attempt += 1


async def _run_connection(url, *, credential, hello, fingerprint, local_factory):
    connection, welcome = await connect_control(
        url,
        credential=credential,
        hello=hello,
        fingerprint=fingerprint,
    )
    try:
        print(f"computer connected; epoch {welcome.connection_id}", flush=True)
        if local_factory is None:

            async def local_factory(alias):
                raise AgentUnavailable("local agent relay is not configured")

        peer = RelayPeer(
            connection,
            welcome.connection_id,
            [agent.alias for agent in hello.agents],
            local_factory=local_factory,
        )
        await peer.run()
    except ProtocolError:
        raise ConnectorAccessError("dispatcher protocol rejected; correct configuration") from None
    except (ConnectionClosed, ConnectionError, TimeoutError):
        if connection.close_code == 1008:
            raise ConnectorAccessError("computer access changed or connection replaced") from None
        raise ConnectorUnavailable("dispatcher connection ended") from None
    else:
        if connection.close_code == 1008:
            raise ConnectorAccessError("computer access changed or connection replaced")
        raise ConnectorUnavailable("dispatcher connection ended")
    finally:
        await connection.close()
