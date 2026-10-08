"""Computer ingress and an outgoing registration client over WS or WSS.

This control channel advertises agents and monitors identity/liveness. Task
relay will use this connection in the next increment; no prompts run here.
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
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from acp_gateway.agents.errors import AgentError
from acp_gateway.agents.tls import fingerprint_of, pin_certificate
from acp_gateway.agents.transport import _NoRedirectConnect
from acp_gateway.connectors.protocol import (
    MAX_FRAME_BYTES,
    AgentManifest,
    Hello,
    Ping,
    Pong,
    ProtocolError,
    Welcome,
    decode_frame,
    encode_frame,
)
from acp_gateway.log import register_secret

# Library DEBUG traces include handshake headers. Never enable wire tracing.
_WIRE_LOG = logging.getLogger("acp_gateway.connectors.wire")
_WIRE_LOG.setLevel(logging.WARNING)


@dataclass
class Registration:
    computer_id: str
    connection_id: UUID
    generation: int
    agents: tuple[AgentManifest, ...]
    connection: object


class ControlDispatcher:
    def __init__(self, registry, *, heartbeat_seconds: int = 20, connect_path: str = "/connect"):
        if not 5 <= heartbeat_seconds <= 300:
            raise ValueError("heartbeat must be between 5 and 300 seconds")
        self.registry = registry
        self.connect_path = validate_connect_path(connect_path)
        self.heartbeat_seconds = heartbeat_seconds
        self.active: dict[str, Registration] = {}

    def authenticate(self, connection, request):
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
                await registration.connection.close(code=1011, reason="access check unavailable")
                return
            if not valid:
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
            self.active[identity] = registration
            if previous is not None:
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
            sequence = 0
            while True:
                await connection.send(encode_frame(Ping(sequence=sequence)))
                frame = decode_frame(
                    await asyncio.wait_for(
                        connection.recv(),
                        timeout=self.heartbeat_seconds,
                    )
                )
                if not isinstance(frame, Pong) or frame.sequence != sequence:
                    raise ProtocolError("invalid heartbeat")
                sequence += 1
                try:
                    await asyncio.wait_for(connection.wait_closed(), timeout=self.heartbeat_seconds)
                    break
                except TimeoutError:
                    pass
        except (ProtocolError, TimeoutError):
            await connection.close(code=1008, reason="control protocol rejected")
        except sqlite3.Error:
            await connection.close(code=1011, reason="access check unavailable")
        except ConnectionClosed:
            pass
        finally:
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
            max_size=MAX_FRAME_BYTES,
            max_queue=4,
            compression=None,
            ping_interval=None,
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
            max_size=MAX_FRAME_BYTES,
            max_queue=4,
            compression=None,
            ping_interval=None,
            close_timeout=2,
            open_timeout=10,
            logger=_WIRE_LOG,
        )
    except (AgentError, OSError, TimeoutError, InvalidHandshake):
        raise ValueError(
            "cannot connect to dispatcher: verify TLS pin, reachability and computer access"
            if parts.scheme == "wss"
            else "cannot connect to dispatcher: verify reachability and computer access"
        ) from None
    try:
        if pin is not None:
            der = connection.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if fingerprint_of(der) != pin.fingerprint:
                raise ValueError("dispatcher TLS certificate changed between probe and connect")
        await connection.send(encode_frame(hello))
        welcome = decode_frame(await asyncio.wait_for(connection.recv(), timeout=5))
        if not isinstance(welcome, Welcome):
            raise ProtocolError("dispatcher did not welcome this computer")
        return connection, welcome
    except BaseException as exc:
        await connection.close()
        if isinstance(exc, (ConnectionClosed, TimeoutError, ProtocolError)):
            raise ValueError("dispatcher rejected computer registration") from None
        raise


async def run_connector(url: str, *, credential: str, hello: Hello, fingerprint: str | None = None):
    connection, welcome = await connect_control(
        url,
        credential=credential,
        hello=hello,
        fingerprint=fingerprint,
    )
    try:
        print(f"computer connected; epoch {welcome.connection_id}; registration only", flush=True)
        while True:
            frame = decode_frame(
                await asyncio.wait_for(
                    connection.recv(),
                    timeout=welcome.heartbeat_seconds * 2 + 5,
                )
            )
            if not isinstance(frame, Ping):
                raise ProtocolError("unexpected dispatcher control frame")
            await connection.send(encode_frame(Pong(sequence=frame.sequence)))
    except (ConnectionClosed, TimeoutError):
        raise ValueError("dispatcher connection ended; reconnect explicitly") from None
    finally:
        await connection.close()
