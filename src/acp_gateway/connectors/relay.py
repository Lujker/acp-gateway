"""One WS reader/writer and isolated, bounded ACP streams per computer epoch."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from websockets.exceptions import ConnectionClosed

from acp_gateway.agents.errors import (
    AgentUnavailable,
    AuthenticationFailed,
    TLSFingerprintMismatch,
)
from acp_gateway.agents.transport import AgentTransport, _acceptable
from acp_gateway.connectors.policy import LocalPolicyError
from acp_gateway.connectors.protocol import (
    MAX_DATA_BYTES,
    Close,
    Data,
    Open,
    Opened,
    Ping,
    Pong,
    ProtocolError,
    decode_frame,
    encode_frame,
)
from acp_gateway.log import get_logger
from acp_gateway.storage.records import utcnow

MAX_STREAMS = 32
MAX_STREAM_IDS = 4096
MAX_QUEUE_BYTES = 2 * MAX_DATA_BYTES
MAX_QUEUE_MESSAGES = 128
MAX_CONTROL_MESSAGES = 64

LocalFactory = Callable[[str], Awaitable[AgentTransport]]


class _Inbox:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=MAX_QUEUE_MESSAGES)
        self.bytes = 0
        self.closed = asyncio.Event()

    def offer(self, message, size):
        if self.closed.is_set():
            return
        if self.bytes + size > MAX_QUEUE_BYTES or self.queue.full():
            raise BufferError("ACP stream queue overflow")
        self.queue.put_nowait((message, size))
        self.bytes += size

    async def receive(self):
        item = await self.queue.get()
        if item is None:
            # EOF remains observable by any later receive.
            self.queue.put_nowait(None)
            return None
        message, size = item
        self.bytes -= size
        return message

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.bytes = 0
        self.queue.put_nowait(None)


class RelayTransport:
    """VPS-side AgentTransport; closure affects only this stream."""

    def __init__(self, peer: RelayPeer, alias: str, stream: UUID):
        self.peer, self.alias, self.stream = peer, alias, stream
        self.inbox = _Inbox()
        self.closed = self.inbox.closed
        self.opened = asyncio.get_running_loop().create_future()

    async def send(self, message):
        if self.closed.is_set() or self.peer.closed.is_set():
            raise ConnectionError("relay stream closed")
        if not _acceptable(message):
            raise ConnectionError("invalid ACP message")
        try:
            await self.peer.send(Data(**self.route, message=message))
        except (BufferError, ProtocolError):
            await self.close(code="overflow")
            raise ConnectionError("ACP relay message exceeds limits") from None

    @property
    def route(self):
        return dict(epoch=self.peer.epoch, alias=self.alias, stream=self.stream)

    async def receive(self):
        return await self.inbox.receive()

    async def close(self, *, code="closed"):
        if self.closed.is_set():
            return
        self.peer.stop(self.stream, code)
        with contextlib.suppress(ConnectionError, BufferError):
            await self.peer.send(Close(**self.route, code=code))


@dataclass
class _LocalStream:
    alias: str
    inbox: _Inbox
    task: asyncio.Task | None = None


class RelayPeer:
    """A computer connection. Only run() reads; only _write() writes WS frames.

    Local stream workers never block the shared reader. Both byte and item
    bounds apply; a slow or oversized stream fails without poisoning others.
    Stream IDs cannot be reused within an epoch.
    """

    def __init__(
        self, connection, epoch: UUID, aliases, *, local_factory: LocalFactory | None = None
    ):
        self.connection, self.epoch = connection, epoch
        self.aliases = frozenset(aliases)
        self.local_factory = local_factory
        self.streams: dict[UUID, RelayTransport | _LocalStream] = {}
        self.seen: set[UUID] = set()
        self.closed = asyncio.Event()
        self.ready = asyncio.Event()
        self.outbox = asyncio.Queue(maxsize=MAX_QUEUE_MESSAGES + MAX_CONTROL_MESSAGES)
        self.out_bytes = 0
        self.out_controls = 0
        self.tasks: set[asyncio.Task] = set()
        self.failures: dict[str, dict] = {}

    def record_failure(self, alias, code):
        # Only bounded, advertised aliases and input-independent codes are retained.
        if alias in self.aliases and code and code != "closed":
            self.failures[alias] = {"code": code, "at": utcnow().isoformat()}

    def route_status(self, alias):
        return {
            "epoch": str(self.epoch),
            "active_streams": sum(s.alias == alias for s in self.streams.values()),
            "capacity_available": len(self.streams) < MAX_STREAMS
            and len(self.seen) < MAX_STREAM_IDS,
            "last_stream_error": self.failures.get(alias),
        }

    async def send(self, frame):
        if self.closed.is_set():
            raise ConnectionError("computer disconnected")
        payload = encode_frame(frame)
        size = len(payload.encode())
        control = not isinstance(frame, Data)
        if (
            self.outbox.full()
            or (control and self.out_controls >= MAX_CONTROL_MESSAGES)
            or (not control and self.out_bytes + size > MAX_QUEUE_BYTES)
        ):
            raise BufferError("relay writer queue overflow")
        done = asyncio.get_running_loop().create_future()
        self.outbox.put_nowait((payload, size, control, done))
        if control:
            self.out_controls += 1
        else:
            self.out_bytes += size
        try:
            await done
        finally:
            # A cancelled caller must not leave an unobserved future exception.
            if not done.done():
                done.cancel()

    async def _write(self):
        while True:
            payload, size, control, done = await self.outbox.get()
            try:
                async with asyncio.timeout(5):
                    await self.connection.send(payload)
            except BaseException:
                if not done.done():
                    done.set_exception(ConnectionError("computer delivery failed"))
                raise
            else:
                if not done.done():
                    done.set_result(None)
            finally:
                if control:
                    self.out_controls -= 1
                else:
                    self.out_bytes -= size

    def invalidate(self):
        """Synchronous fencing for replacement and in-process revocation."""
        if self.closed.is_set():
            return
        self.closed.set()
        self.ready.set()
        for stream in list(self.streams):
            self.stop(stream)
        while not self.outbox.empty():
            _, _, _, done = self.outbox.get_nowait()
            if not done.done():
                done.set_exception(ConnectionError("computer disconnected"))

    def stop(self, stream_id: UUID, code=None):
        stream = self.streams.pop(stream_id, None)
        if stream is None:
            return
        self.record_failure(stream.alias, code)
        if isinstance(stream, RelayTransport):
            stream.inbox.close()
            if not stream.opened.done():
                error = (
                    AuthenticationFailed("computer rejected agent access")
                    if code in {"access_denied", "policy_denied"}
                    else AgentUnavailable("remote agent stream is unavailable")
                )
                stream.opened.set_exception(error)
        else:
            stream.inbox.close()
            if stream.task is not None:
                stream.task.cancel()

    async def open(self, alias: str) -> RelayTransport:
        await self.ready.wait()
        if self.closed.is_set() or alias not in self.aliases:
            raise AgentUnavailable("computer or advertised agent is offline")
        if len(self.streams) >= MAX_STREAMS or len(self.seen) >= MAX_STREAM_IDS:
            self.record_failure(alias, "stream_limit")
            raise AgentUnavailable("computer relay stream limit reached")
        stream_id = uuid4()
        transport = RelayTransport(self, alias, stream_id)
        self.streams[stream_id] = transport
        self.seen.add(stream_id)
        previous_failure = self.failures.get(alias)
        try:
            async with asyncio.timeout(15):
                await self.send(Open(**transport.route))
                await asyncio.shield(transport.opened)
            return transport
        except BaseException as exc:
            await transport.close()
            if transport.opened.done() and not transport.opened.cancelled():
                transport.opened.exception()
            if isinstance(exc, (ConnectionError, TimeoutError)):
                if self.failures.get(alias) is previous_failure:
                    self.record_failure(alias, "open_failed")
                raise AgentUnavailable("computer stream could not be opened") from None
            raise

    async def run(self):
        writer = asyncio.create_task(self._write())
        reader = asyncio.create_task(self._read())
        self.ready.set()
        try:
            done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.invalidate()
            for task in (reader, writer, *self.tasks):
                task.cancel()
            await asyncio.gather(reader, writer, *self.tasks, return_exceptions=True)

    async def _read(self):
        async for payload in self.connection:
            frame = decode_frame(payload)
            if isinstance(frame, Ping) and self.local_factory is not None:
                await self.send(Pong(sequence=frame.sequence))
                continue
            if isinstance(frame, Pong) and self.local_factory is None and frame.sequence == 0:
                # A single v1 compatibility probe; liveness uses native WS ping/pong.
                continue
            if not isinstance(frame, (Open, Opened, Data, Close)) or frame.epoch != self.epoch:
                raise ProtocolError("invalid relay epoch or frame")
            route = dict(epoch=self.epoch, alias=frame.alias, stream=frame.stream)
            if isinstance(frame, Open):
                if self.local_factory is None or frame.stream in self.seen:
                    raise ProtocolError("invalid stream open")
                if (
                    frame.alias not in self.aliases
                    or len(self.tasks) >= MAX_STREAMS
                    or len(self.seen) >= MAX_STREAM_IDS
                ):
                    await self.send(Close(**route, code="access_denied"))
                    continue
                self.seen.add(frame.stream)
                stream = _LocalStream(frame.alias, _Inbox())
                self.streams[frame.stream] = stream
                task = asyncio.create_task(self._local(frame.stream, stream))
                stream.task = task
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                continue
            stream = self.streams.get(frame.stream)
            if stream is None:
                # Late data/close from a retired stream must never reach a new one.
                continue
            if stream.alias != frame.alias:
                raise ProtocolError("invalid stream alias")
            if isinstance(frame, Close):
                self.stop(frame.stream, frame.code)
            elif isinstance(frame, Opened):
                if not isinstance(stream, RelayTransport) or stream.opened.done():
                    raise ProtocolError("unexpected stream acknowledgement")
                stream.opened.set_result(None)
                self.failures.pop(frame.alias, None)
            else:
                if not _acceptable(frame.message):
                    self.stop(frame.stream, "policy_denied")
                    await self.send(Close(**route, code="policy_denied"))
                    continue
                if isinstance(stream, RelayTransport) and not stream.opened.done():
                    raise ProtocolError("agent data before stream acknowledgement")
                try:
                    stream.inbox.offer(
                        frame.message,
                        len(payload.encode() if isinstance(payload, str) else payload),
                    )
                except BufferError:
                    self.stop(frame.stream, "overflow")
                    await self.send(Close(**route, code="overflow"))

    async def _local(self, stream_id, stream):
        transport = None
        pumps = []
        route = dict(epoch=self.epoch, alias=stream.alias, stream=stream_id)
        code = "unavailable"
        try:
            async with asyncio.timeout(15):
                transport = await self.local_factory(stream.alias)
            await self.send(Opened(**route))

            async def to_agent():
                while (message := await stream.inbox.receive()) is not None:
                    await transport.send(message)

            async def from_agent():
                while (message := await transport.receive()) is not None:
                    if not _acceptable(message):
                        raise LocalPolicyError("invalid agent response")
                    await self.send(Data(**route, message=message))

            pumps = [asyncio.create_task(to_agent()), asyncio.create_task(from_agent())]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (AuthenticationFailed, TLSFingerprintMismatch):
            code = "access_denied"
        except LocalPolicyError:
            code = "policy_denied"
        except (BufferError, ProtocolError):
            code = "overflow"
        except (AgentUnavailable, ConnectionError, ConnectionClosed, TimeoutError, OSError):
            pass
        except Exception as exc:
            # Keep a faulty local adapter isolated and never echo its input/secrets.
            get_logger(__name__).warning("local relay stream failed", error=type(exc).__name__)
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            if transport is not None:
                with contextlib.suppress(Exception):
                    await transport.close()
            # Do not cancel ourselves through stop().
            if self.streams.get(stream_id) is stream:
                self.streams.pop(stream_id)
            stream.inbox.close()
            with contextlib.suppress(ConnectionError, BufferError):
                await self.send(Close(**route, code=code))
