"""One WS reader/writer and isolated, bounded ACP streams per computer epoch."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
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
    Ack,
    Close,
    Data,
    Open,
    Opened,
    Ping,
    Pong,
    ProfilesRequest,
    ProfilesResponse,
    ProfilesResult,
    ProtocolError,
    Resume,
    Resumed,
    ShellRequest,
    ShellResponse,
    ShellResult,
    UpdateInfoRequest,
    UpdateInfoResponse,
    UpdateRequest,
    UpdateResponse,
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
# A resumable stream survives a computer connection drop for this long. The
# local agent keeps working; unacknowledged data is replayed after reconnect.
RESUME_GRACE = 300.0
# After reconnecting, the dispatcher must reclaim parked streams this quickly.
RESUME_WINDOW = 30.0
ACK_EVERY = 16
MAX_REPLAY_MESSAGES = 4096
MAX_REPLAY_BYTES = 16 * MAX_DATA_BYTES

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


class _Link:
    """One relay stream. A resumable stream numbers its data and keeps it until
    acknowledged, so it can move to the next connection epoch of the same computer.
    """

    def __init__(self, peer: RelayPeer, alias: str, stream: UUID):
        self.peer: RelayPeer | None = peer
        self.alias, self.stream, self.epoch = alias, stream, peer.epoch
        self.resumable = peer.stream_resume
        self.inbox = _Inbox()
        self.closed = self.inbox.closed
        self.established = False
        self.parking: StreamParking | None = None
        self.parked_at: float | None = None
        self.sent = 0
        self.received = 0
        self.unacked: deque = deque()
        self.unacked_bytes = 0
        self.since_ack = 0
        self.lock = asyncio.Lock()

    @property
    def route(self):
        return dict(epoch=self.epoch, alias=self.alias, stream=self.stream)

    def acked(self, received):
        if received > self.sent:
            raise ProtocolError("invalid relay acknowledgement")
        while self.unacked and self.unacked[0][0] <= received:
            self.unacked_bytes -= self.unacked.popleft()[2]

    def accept(self, seq):
        """True for the next message, False for a replayed duplicate."""
        if not self.resumable:
            if seq is not None:
                raise ProtocolError("unnegotiated relay sequence")
            return True
        if seq is None or seq > self.received + 1:
            raise ProtocolError("invalid relay sequence")
        if seq <= self.received:
            return False
        self.received = seq
        return True

    def ack_due(self):
        if not self.resumable:
            return False
        self.since_ack += 1
        if self.since_ack < ACK_EVERY:
            return False
        self.since_ack = 0
        return True

    async def deliver(self, message):
        async with self.lock:
            if self.closed.is_set():
                raise ConnectionError("relay stream closed")
            peer = self.peer
            if not self.resumable:
                await peer.send(Data(**self.route, message=message))
                return
            # Envelope margin; the writer still enforces the exact frame bound.
            size = len(json.dumps(message, ensure_ascii=False).encode()) + 512
            if size > MAX_DATA_BYTES:
                raise ProtocolError("ACP relay message exceeds limits")
            if (
                len(self.unacked) >= MAX_REPLAY_MESSAGES
                or self.unacked_bytes + size > MAX_REPLAY_BYTES
            ):
                raise BufferError("relay replay buffer overflow")
            self.sent += 1
            self.unacked.append((self.sent, message, size))
            self.unacked_bytes += size
            if peer is None:
                return  # Parked: replayed after the computer reconnects.
            try:
                await peer.send(Data(**self.route, seq=self.sent, message=message))
            except ConnectionError:
                if self.closed.is_set():
                    raise
                # Connection lost, stream parked: the message stays queued for replay.

    async def replay(self, peer: RelayPeer, *, resumed=False):
        """Attach to ``peer`` and resend unacknowledged data in order."""
        async with self.lock:
            if (
                self.closed.is_set()
                or peer.closed.is_set()
                or peer.streams.get(self.stream) is not self
            ):
                return
            self.peer, self.epoch = peer, peer.epoch
            if resumed:
                await peer.send(Resumed(**self.route, received=self.received))
            for seq, message, _ in list(self.unacked):
                await peer.send(Data(**self.route, seq=seq, message=message))

    def halt(self, code=None):
        raise NotImplementedError


class StreamParking:
    """Streams waiting for the next connection of the same computer."""

    def __init__(self, grace: float = RESUME_GRACE, *, generation=None):
        self.grace, self.generation = grace, generation
        self._links: dict[UUID, tuple[_Link, asyncio.TimerHandle]] = {}

    def __len__(self):
        return len(self._links)

    def links(self):
        return [link for link, _ in self._links.values()]

    def park(self, links):
        loop = asyncio.get_running_loop()
        for link in links:
            link.peer, link.parking = None, self
            if link.parked_at is None:
                link.parked_at = loop.time()
            self._schedule(link, link.parked_at + self.grace - loop.time())
        if links:
            get_logger(__name__).info("relay streams parked", streams=len(links))

    def _schedule(self, link, delay):
        previous = self._links.get(link.stream)
        if previous is not None:
            previous[1].cancel()
        handle = asyncio.get_running_loop().call_later(max(0, delay), self.expire, link.stream)
        self._links[link.stream] = (link, handle)

    def shorten(self, seconds):
        loop = asyncio.get_running_loop()
        for link, handle in list(self._links.values()):
            remaining = handle.when() - loop.time()
            if remaining > seconds:
                self._schedule(link, seconds)

    def take(self, stream: UUID) -> _Link | None:
        entry = self._links.pop(stream, None)
        if entry is None:
            return None
        link, handle = entry
        handle.cancel()
        link.parking, link.parked_at = None, None
        return link

    def discard(self, link):
        entry = self._links.get(link.stream)
        if entry is not None and entry[0] is link:
            self.take(link.stream)

    def expire(self, stream: UUID):
        link = self.take(stream)
        if link is not None:
            get_logger(__name__).warning("parked relay stream expired")
            link.halt("unavailable")

    def close_all(self):
        for stream in list(self._links):
            link = self.take(stream)
            if link is not None:
                link.halt("unavailable")


class RelayTransport(_Link):
    """VPS-side AgentTransport; closure affects only this stream."""

    def __init__(self, peer: RelayPeer, alias: str, stream: UUID):
        super().__init__(peer, alias, stream)
        self.opened = asyncio.get_running_loop().create_future()

    async def send(self, message):
        if self.closed.is_set():
            raise ConnectionError("relay stream closed")
        if not _acceptable(message):
            raise ConnectionError("invalid ACP message")
        try:
            await self.deliver(message)
        except (BufferError, ProtocolError):
            await self.close(code="overflow")
            raise ConnectionError("ACP relay message exceeds limits") from None

    async def receive(self):
        return await self.inbox.receive()

    def halt(self, code=None):
        self.inbox.close()
        if not self.opened.done():
            error = (
                AuthenticationFailed("computer rejected agent access")
                if code in {"access_denied", "policy_denied"}
                else AgentUnavailable("remote agent stream is unavailable")
            )
            self.opened.set_exception(error)

    async def close(self, *, code="closed"):
        if self.closed.is_set():
            return
        peer = self.peer
        if peer is None or peer.streams.get(self.stream) is not self:
            # Parked or still resuming: nothing to tell; the computer expires its copy.
            if self.parking is not None:
                self.parking.discard(self)
            self.halt(code)
            return
        route = self.route
        peer.stop(self.stream, code)
        with contextlib.suppress(ConnectionError, BufferError):
            await peer.send(Close(**route, code=code))


class _LocalStream(_Link):
    """Computer-side stream owning one local agent transport task."""

    task: asyncio.Task | None = None

    def halt(self, code=None):
        self.inbox.close()
        if self.task is not None:
            self.task.cancel()


class RelayPeer:
    """A computer connection. Only run() reads; only _write() writes WS frames.

    Local stream workers never block the shared reader. Both byte and item
    bounds apply; a slow or oversized stream fails without poisoning others.
    Stream IDs cannot be reused within an epoch.
    """

    def __init__(
        self,
        connection,
        epoch: UUID,
        aliases,
        *,
        local_factory: LocalFactory | None = None,
        update_inventory=False,
        installation_report=None,
        update_operations=False,
        update_handler=None,
        agent_profiles=False,
        agent_discovery=False,
        profiles_handler=None,
        shell_operations=False,
        shell_handler=None,
        stream_resume=False,
        parking: StreamParking | None = None,
        on_park: Callable[[list[_Link]], None] | None = None,
        pending_resume: StreamParking | None = None,
    ):
        self.connection, self.epoch = connection, epoch
        self.aliases = frozenset(aliases)
        self.local_factory = local_factory
        self.update_inventory = update_inventory
        self.installation_report = installation_report
        self.update_operations = update_operations
        self.update_handler = update_handler
        self.agent_profiles = agent_profiles
        self.agent_discovery = agent_discovery
        self.profiles_handler = profiles_handler
        self.shell_operations = shell_operations
        self.shell_handler = shell_handler
        self.shell_requests = {}
        self.shell_workers = set()
        # Resume is opt-in per connection; old peers keep close-on-drop streams.
        self.stream_resume = stream_resume
        self.parking = parking  # computer side: streams this connection may reclaim
        self.on_park = on_park if stream_resume else None
        self.pending_resume = pending_resume if stream_resume else None  # dispatcher side
        self.resuming: dict[UUID, _Link] = {}
        self.update_requests = {}
        self.update_kinds = {}
        self.update_workers = set()
        self.streams: dict[UUID, _Link] = {}
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

    def invalidate(self, *, park=True):
        """Synchronous fencing for loss, replacement and in-process revocation.

        On connection loss established resumable streams are parked for the next
        epoch; revocation passes ``park=False`` and closes every stream.
        """
        if self.closed.is_set():
            return
        self.closed.set()
        self.ready.set()
        for pending in self.update_requests.values():
            if not pending.done():
                pending.set_exception(ConnectionError("computer disconnected"))
        for _, pending in self.shell_requests.values():
            if not pending.done():
                pending.set_exception(
                    ConnectionError("computer disconnected; inspect shell status")
                )
        parked = []
        for stream_id in list(self.streams):
            link = self.streams[stream_id]
            if (
                park
                and self.on_park is not None
                and link.resumable
                and link.established
                and not link.closed.is_set()
            ):
                del self.streams[stream_id]
                task = getattr(link, "task", None)
                if task is not None:
                    self.tasks.discard(task)
                parked.append(link)
            else:
                self.stop(stream_id)
        self.resuming.clear()
        if parked:
            self.on_park(parked)
        while not self.outbox.empty():
            _, _, _, done = self.outbox.get_nowait()
            if not done.done():
                done.set_exception(ConnectionError("computer disconnected"))

    def stop(self, stream_id: UUID, code=None):
        stream = self.streams.pop(stream_id, None)
        if stream is None:
            return
        self.record_failure(stream.alias, code)
        stream.halt(code)

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
        if self.pending_resume is not None and len(self.pending_resume):
            task = asyncio.create_task(self._resume_parked(self.pending_resume))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        try:
            done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            # Park only on abnormal loss. A clean close (1000/1001) is a deliberate
            # stop of either side; 1008 means access changed or a replacement.
            code = getattr(self.connection, "close_code", None)
            self.invalidate(park=code not in (1000, 1001, 1008))
            for task in (reader, writer, *self.tasks):
                task.cancel()
            await asyncio.gather(reader, writer, *self.tasks, return_exceptions=True)

    async def _read(self):
        async for payload in self.connection:
            frame = decode_frame(payload)
            if isinstance(frame, (ShellRequest, ShellResponse)):
                if not self.shell_operations or frame.epoch != self.epoch:
                    raise ProtocolError("unnegotiated shell operations or invalid epoch")
                if isinstance(frame, ShellResponse):
                    if self.local_factory is not None:
                        raise ProtocolError("unexpected shell response")
                    pending = self.shell_requests.get(frame.request)
                    if pending is not None and not pending[1].done():
                        if pending[0] != frame.result.operation:
                            raise ProtocolError("shell operation response mismatch")
                        pending[1].set_result(frame.result)
                else:
                    if self.local_factory is None:
                        raise ProtocolError("invalid shell request direction")
                    if len(self.shell_workers) >= 4:
                        await self.send(
                            ShellResponse(
                                epoch=self.epoch,
                                request=frame.request,
                                result=ShellResult(
                                    operation=frame.operation, state="refused", reason="busy"
                                ),
                            )
                        )
                        continue
                    task = asyncio.create_task(self._operate_shell(frame))
                    self.tasks.add(task)
                    self.shell_workers.add(task)
                    task.add_done_callback(self.tasks.discard)
                    task.add_done_callback(self.shell_workers.discard)
                continue
            if isinstance(frame, (ProfilesRequest, ProfilesResponse)):
                if not self.agent_profiles or frame.epoch != self.epoch:
                    raise ProtocolError("unnegotiated profiles or invalid epoch")
                if (
                    isinstance(frame, ProfilesRequest)
                    and (frame.action == "discover" or (frame.profile and frame.profile.candidate))
                    and not self.agent_discovery
                ):
                    raise ProtocolError("unnegotiated discovery")
                if isinstance(frame, ProfilesResponse):
                    if self.local_factory is not None:
                        raise ProtocolError("unexpected profile response")
                    pending = self.update_requests.get(frame.request)
                    if pending is not None and not pending.done():
                        if self.update_kinds[frame.request] is not ProfilesResponse:
                            raise ProtocolError("profile response type mismatch")
                        pending.set_result(frame.result)
                else:
                    if self.local_factory is None:
                        raise ProtocolError("invalid profile request")
                    if self.update_workers:
                        await self.send(
                            ProfilesResponse(
                                epoch=self.epoch,
                                request=frame.request,
                                result=ProfilesResult(status="busy"),
                            )
                        )
                        continue
                    task = asyncio.create_task(self._operate_profiles(frame))
                    self.tasks.add(task)
                    self.update_workers.add(task)
                    task.add_done_callback(self.tasks.discard)
                    task.add_done_callback(self.update_workers.discard)
                continue
            if isinstance(
                frame, (UpdateInfoRequest, UpdateInfoResponse, UpdateRequest, UpdateResponse)
            ):
                if not self.update_inventory or frame.epoch != self.epoch:
                    raise ProtocolError("unnegotiated update inventory or invalid epoch")
                if (
                    isinstance(frame, (UpdateRequest, UpdateResponse))
                    and not self.update_operations
                ):
                    raise ProtocolError("unnegotiated update operation")
                if isinstance(frame, (UpdateInfoResponse, UpdateResponse)):
                    if self.local_factory is not None:
                        raise ProtocolError("unexpected update inventory response")
                    pending = self.update_requests.get(frame.request)
                    if pending is not None and not pending.done():
                        if type(frame) is not self.update_kinds[frame.request]:
                            raise ProtocolError("update response type mismatch")
                        pending.set_result(
                            frame.report if isinstance(frame, UpdateInfoResponse) else frame.result
                        )
                else:
                    if self.local_factory is None or len(self.update_workers) >= 4:
                        raise ProtocolError("invalid update inventory request")
                    task = asyncio.create_task(
                        self._operate_update(frame)
                        if isinstance(frame, UpdateRequest)
                        else self._report_installation(frame)
                    )
                    self.tasks.add(task)
                    self.update_workers.add(task)
                    task.add_done_callback(self.tasks.discard)
                    task.add_done_callback(self.update_workers.discard)
                continue
            if isinstance(frame, Ping) and self.local_factory is not None:
                await self.send(Pong(sequence=frame.sequence))
                continue
            if isinstance(frame, Pong) and self.local_factory is None and frame.sequence == 0:
                # A single v1 compatibility probe; liveness uses native WS ping/pong.
                continue
            if (
                not isinstance(frame, (Open, Opened, Data, Close, Ack, Resume, Resumed))
                or frame.epoch != self.epoch
            ):
                raise ProtocolError("invalid relay epoch or frame")
            route = dict(epoch=self.epoch, alias=frame.alias, stream=frame.stream)
            if isinstance(frame, (Ack, Resume, Resumed)) and not self.stream_resume:
                raise ProtocolError("unnegotiated stream resume")
            if isinstance(frame, Resume):
                await self._reclaim(frame, route)
                continue
            if isinstance(frame, Resumed):
                await self._resumed(frame, route)
                continue
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
                stream = _LocalStream(self, frame.alias, frame.stream)
                self.streams[frame.stream] = stream
                task = asyncio.create_task(self._local(frame.stream, stream))
                stream.task = task
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                continue
            if isinstance(frame, Close) and frame.stream in self.resuming:
                # The computer no longer has this stream (e.g. it restarted): fail fast.
                link = self.resuming.pop(frame.stream)
                if link.alias != frame.alias:
                    raise ProtocolError("invalid stream alias")
                if link.parking is not None:
                    link.parking.expire(frame.stream)
                continue
            stream = self.streams.get(frame.stream)
            if stream is None:
                # Late data/close from a retired stream must never reach a new one.
                continue
            if stream.alias != frame.alias:
                raise ProtocolError("invalid stream alias")
            if isinstance(frame, Close):
                self.stop(frame.stream, frame.code)
            elif isinstance(frame, Ack):
                stream.acked(frame.received)
            elif isinstance(frame, Opened):
                if not isinstance(stream, RelayTransport) or stream.opened.done():
                    raise ProtocolError("unexpected stream acknowledgement")
                stream.opened.set_result(None)
                stream.established = True
                self.failures.pop(frame.alias, None)
            else:
                if not _acceptable(frame.message):
                    self.stop(frame.stream, "policy_denied")
                    await self.send(Close(**route, code="policy_denied"))
                    continue
                if isinstance(stream, RelayTransport) and not stream.opened.done():
                    raise ProtocolError("agent data before stream acknowledgement")
                if not stream.accept(frame.seq):
                    continue  # Replayed after resume; already delivered.
                try:
                    stream.inbox.offer(
                        frame.message,
                        len(payload.encode() if isinstance(payload, str) else payload),
                    )
                except BufferError:
                    self.stop(frame.stream, "overflow")
                    await self.send(Close(**route, code="overflow"))
                    continue
                if stream.ack_due():
                    await self.send(Ack(**route, received=stream.received))

    async def _resume_parked(self, parking: StreamParking):
        """Dispatcher side: ask the reconnected computer for each parked stream."""
        for link in parking.links():
            if self.closed.is_set():
                return
            if link.alias not in self.aliases:
                parking.expire(link.stream)
                continue
            if link.stream in self.seen or link.stream in self.resuming:
                continue
            self.seen.add(link.stream)
            self.resuming[link.stream] = link
            try:
                await self.send(
                    Resume(
                        epoch=self.epoch,
                        alias=link.alias,
                        stream=link.stream,
                        received=link.received,
                    )
                )
            except (ConnectionError, BufferError):
                return

    async def _reclaim(self, frame, route):
        """Computer side: move a parked local stream to this epoch."""
        if self.local_factory is None or frame.stream in self.seen:
            raise ProtocolError("invalid stream resume")
        self.seen.add(frame.stream)
        link = self.parking.take(frame.stream) if self.parking is not None else None
        if link is None or link.alias != frame.alias or link.closed.is_set():
            if link is not None:
                link.halt("unavailable")
            await self.send(Close(**route, code="unavailable"))
            return
        link.acked(frame.received)
        self.streams[frame.stream] = link
        self.tasks.add(link.task)
        link.task.add_done_callback(self.tasks.discard)
        self._spawn_replay(link, resumed=True)

    async def _resumed(self, frame, route):
        """Dispatcher side: the computer still runs this stream; replay our side."""
        link = self.resuming.pop(frame.stream, None)
        if self.local_factory is not None or link is None or link.alias != frame.alias:
            raise ProtocolError("unexpected stream resume")
        parking = link.parking
        if link.closed.is_set() or parking is None or parking.take(frame.stream) is not link:
            await self.send(Close(**route, code="closed"))
            return
        link.acked(frame.received)
        self.streams[frame.stream] = link
        self._spawn_replay(link)
        get_logger(__name__).info("relay stream resumed")

    def _spawn_replay(self, link, *, resumed=False):
        async def replay():
            with contextlib.suppress(ConnectionError, BufferError, ProtocolError):
                await link.replay(self, resumed=resumed)

        task = asyncio.create_task(replay())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def update_info(self, *, timeout=5):
        if self.local_factory is not None or not self.update_inventory:
            raise AgentUnavailable("update inventory is unsupported; upgrade connector locally")
        if len(self.update_requests) >= 4:
            raise AgentUnavailable("update inventory is busy")
        return await self._update_rpc(UpdateInfoRequest(epoch=self.epoch, request=uuid4()), timeout)

    async def profiles(self, frame, *, timeout=25):
        if self.local_factory is not None or not self.agent_profiles:
            raise AgentUnavailable("profile management unavailable; enable it locally")
        if (
            frame.action == "discover" or (frame.profile and frame.profile.candidate)
        ) and not self.agent_discovery:
            raise AgentUnavailable("agent discovery unavailable; upgrade connector locally")
        return await self._update_rpc(frame.model_copy(update={"epoch": self.epoch}), timeout)

    async def shell(self, frame, *, timeout=10):
        if self.local_factory is not None or not self.shell_operations:
            raise AgentUnavailable("direct shell unavailable; upgrade connector locally")
        if frame.action == "start" and frame.alias not in self.aliases:
            raise AgentUnavailable("shell agent is not advertised by this computer")
        if len(self.shell_requests) >= 4 or frame.request in self.shell_requests:
            raise AgentUnavailable("shell control requests are busy")
        pending = asyncio.get_running_loop().create_future()
        self.shell_requests[frame.request] = (frame.operation, pending)
        try:
            async with asyncio.timeout(timeout):
                await self.send(frame.model_copy(update={"epoch": self.epoch}))
                return await pending
        finally:
            self.shell_requests.pop(frame.request, None)
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()

    async def _operate_shell(self, request):
        result = ShellResult(operation=request.operation, state="refused", reason="disabled")
        try:
            if self.shell_handler is not None:
                result = await self.shell_handler(request)
                if not isinstance(result, ShellResult) or result.operation != request.operation:
                    raise ValueError("invalid local shell result")
        except Exception:
            # A lost result does not prove that execution never started.
            result = ShellResult(
                operation=request.operation, state="unknown", reason="execution_failed"
            )
            get_logger(__name__).warning("shell outcome unavailable; inspect operation")
        try:
            await self.send(ShellResponse(epoch=self.epoch, request=request.request, result=result))
        except (OSError, ValueError, ConnectionError, BufferError):
            get_logger(__name__).warning("shell response unavailable; no execution replay")

    async def _operate_profiles(self, request):
        result = ProfilesResult(status="disabled")
        try:
            if self.profiles_handler is not None:
                result = await self.profiles_handler(request)
            if result.status == "ok":
                self.aliases = frozenset(a.alias for a in result.agents)
            await self.send(
                ProfilesResponse(epoch=self.epoch, request=request.request, result=result)
            )
        except (OSError, ValueError, ConnectionError, BufferError):
            get_logger(__name__).warning("profile response unavailable")

    async def update_operation(self, frame, *, timeout=10):
        if self.local_factory is not None or not self.update_operations:
            raise AgentUnavailable("remote update is unsupported")
        return await self._update_rpc(frame.model_copy(update={"epoch": self.epoch}), timeout)

    async def _update_rpc(self, frame, timeout):
        if len(self.update_requests) >= 4:
            raise AgentUnavailable("update requests are busy")
        request = frame.request
        pending = asyncio.get_running_loop().create_future()
        self.update_requests[request] = pending
        self.update_kinds[request] = (
            ProfilesResponse
            if isinstance(frame, ProfilesRequest)
            else UpdateResponse
            if isinstance(frame, UpdateRequest)
            else UpdateInfoResponse
        )
        try:
            async with asyncio.timeout(timeout):
                await self.send(frame)
                return await pending
        finally:
            self.update_requests.pop(request, None)
            self.update_kinds.pop(request, None)
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()

    async def _report_installation(self, request):
        report = None
        try:
            if self.installation_report is not None:
                report = await asyncio.to_thread(self.installation_report)
                if report is not None and not self.update_operations:
                    report = report.model_copy(update={"can_update": False})
        except (OSError, ValueError):
            get_logger(__name__).warning("update inventory report unavailable")
        try:
            await self.send(
                UpdateInfoResponse(
                    epoch=self.epoch,
                    request=request.request,
                    report=report,
                )
            )
        except (OSError, ValueError, ConnectionError, BufferError):
            get_logger(__name__).warning("update inventory report unavailable")

    async def _operate_update(self, request):
        from acp_gateway.connectors.protocol import UpdateOperation

        result = UpdateOperation(operation=request.operation, status="failed", reason="unsupported")
        try:
            if self.update_handler is not None:
                result = await asyncio.to_thread(self.update_handler, request)
            await self.send(
                UpdateResponse(epoch=self.epoch, request=request.request, result=result)
            )
        except (OSError, ValueError, ConnectionError, BufferError, sqlite3.Error):
            get_logger(__name__).warning("update operation response unavailable")

    async def _local(self, stream_id, stream):
        transport = None
        pumps = []
        code = "unavailable"
        try:
            async with asyncio.timeout(15):
                transport = await self.local_factory(stream.alias)
            await self.send(Opened(**stream.route))
            stream.established = True

            async def to_agent():
                while (message := await stream.inbox.receive()) is not None:
                    await transport.send(message)

            async def from_agent():
                while (message := await transport.receive()) is not None:
                    if not _acceptable(message):
                        raise LocalPolicyError("invalid agent response")
                    # Parked streams keep draining the agent into the replay buffer.
                    await stream.deliver(message)

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
            # The stream may have moved to a later epoch or be parked.
            peer = stream.peer
            if stream.parking is not None:
                stream.parking.discard(stream)
            # Do not cancel ourselves through stop().
            if peer is not None and peer.streams.get(stream_id) is stream:
                peer.streams.pop(stream_id)
            stream.inbox.close()
            if peer is not None:
                with contextlib.suppress(ConnectionError, BufferError):
                    await peer.send(Close(**stream.route, code=code))
