"""AgentClient: one ACP connection to one agent profile.

Responsibilities: connect (TLS pinning, auth header, ``initialize`` with every
client capability disabled), reconnect with backoff, create and load sessions
and put them into the profile's ``session_mode``, run prompt turns as a stream
of normalized events, cancel turns, and route permission requests to a
handler supplied by the caller.

Not here: session-to-channel mapping, policy and approval routing — that is
the gateway core (P1.3, P1.4).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from acp import PROTOCOL_VERSION, RequestError, connect_to_agent
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    FileSystemCapabilities,
    Implementation,
    RequestPermissionResponse,
    SessionModeState,
    TextContentBlock,
)
from pydantic import SecretStr, ValidationError

from acp_gateway import __version__
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
from acp_gateway.agents.events import AgentEvent, MessageChunk, TurnFinished, normalize
from acp_gateway.agents.tls import TlsPin, pin_certificate
from acp_gateway.agents.transport import PinnedWebSocketTransport
from acp_gateway.config import AgentProfile
from acp_gateway.log import get_logger

T = TypeVar("T")

DEFAULT_RECONNECT_DELAYS: tuple[float, ...] = (1, 2, 5, 10, 30)
DEFAULT_CANCEL_TIMEOUT = 5.0


@dataclass(frozen=True)
class PermissionOption:
    option_id: str
    name: str
    kind: str


@dataclass(frozen=True)
class PermissionRequest:
    """What the agent wants to do; show ``raw_input`` to the human, not the original ask."""

    session_id: str
    tool_call_id: str
    title: str | None
    kind: str | None
    raw_input: Any
    options: tuple[PermissionOption, ...]

    def option(self, kind: str) -> PermissionOption | None:
        return next((o for o in self.options if o.kind == kind), None)


# Returns the chosen option id, or None for "cancelled".
PermissionHandler = Callable[[PermissionRequest], Awaitable[str | None]]


async def reject_all(request: PermissionRequest) -> str | None:
    """Default handler: deny every action (used until an approver is wired in)."""
    option = request.option("reject_once")
    return option.option_id if option else None


@dataclass(frozen=True)
class AgentReply:
    session_id: str
    text: str
    stop_reason: str
    usage: dict[str, Any] | None


class _TurnDone:
    """Queue sentinel: the prompt request has completed."""


class _AcpClientSide:
    """The ACP ``Client`` interface; delegates to :class:`AgentClient`.

    File system and terminal methods are not advertised in ``initialize`` and
    answer "method not found" if an agent calls them anyway.
    """

    def __init__(self, owner: AgentClient) -> None:
        self._owner = owner

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._owner._on_update(session_id, update)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> RequestPermissionResponse:
        return await self._owner._on_permission(session_id, tool_call, options)

    async def _refuse(self, method: str) -> Any:
        self._owner._log.warning("agent called a client capability it was not given", method=method)
        raise RequestError.method_not_found(method)

    async def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("fs/write_text_file")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("fs/read_text_file")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("terminal/create")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("terminal/output")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("terminal/release")

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("terminal/wait_for_exit")

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("terminal/kill")

    async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("elicitation/create")

    async def complete_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        return await self._refuse("elicitation/complete")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    def on_connect(self, conn: Any) -> None:
        return None


class AgentClient:
    def __init__(
        self,
        profile: AgentProfile,
        secret: SecretStr | None = None,
        *,
        pin_dir: Path,
        permission_handler: PermissionHandler = reject_all,
        open_timeout: float = 15,
        request_timeout: float = 60,
        reconnect_delays: Sequence[float] = DEFAULT_RECONNECT_DELAYS,
        client_name: str = "acp-gateway",
    ) -> None:
        self.profile = profile
        self._secret = secret
        self._pin_file = pin_dir / f"{profile.alias}.sha256"
        self._permission_handler = permission_handler
        self._open_timeout = open_timeout
        self._request_timeout = request_timeout
        self._reconnect_delays = tuple(reconnect_delays)
        self._client_name = client_name
        self._log = get_logger(__name__).bind(agent=profile.alias)

        self._conn: Any = None
        self._transport: PinnedWebSocketTransport | None = None
        self._connect_lock = asyncio.Lock()
        self._attached: set[str] = set()  # sessions usable on the current connection
        self._uncertain: set[str] = set()  # cancellation not confirmed on this connection
        self._session_cwds: dict[str, str] = {}
        self._loading: set[str] = set()  # sessions whose history replay is suppressed
        self._turns: dict[str, asyncio.Queue[AgentEvent | _TurnDone]] = {}
        self._permission_tasks: defaultdict[str, set[asyncio.Task[str | None]]] = defaultdict(set)
        self._watchers: set[asyncio.Task[None]] = set()
        self._requests = 0  # requests in flight outside prompt turns

        self.agent_info: dict[str, Any] | None = None
        self.agent_capabilities: Any = None
        self.tls_pin: TlsPin | None = None

    # ------------------------------------------------------------- connection

    @property
    def connected(self) -> bool:
        return (
            self._conn is not None
            and self._transport is not None
            and not self._transport.closed.is_set()
        )

    async def connect(self) -> None:
        """Connect once; raises a normalized :class:`AgentError` on failure."""
        async with self._connect_lock:
            if not self.connected:
                await self._connect_once()

    async def ensure_connected(self) -> None:
        """Connect, retrying with backoff; authentication and TLS pin errors are not retried."""
        if self.connected:
            return
        async with self._connect_lock:
            if self.connected:
                return
            last: AgentError | None = None
            for delay in (0, *self._reconnect_delays):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await self._connect_once()
                    return
                except (AuthenticationFailed, TLSFingerprintMismatch):
                    raise
                except AgentUnavailable as exc:
                    last = exc
                    self._log.warning("agent connection attempt failed", error=str(exc))
            raise AgentUnavailable(f"{self.profile.display_name} is unavailable: {last}") from last

    async def _connect_once(self) -> None:
        endpoint = self.profile.acp_endpoint
        parts = urlsplit(endpoint)
        pin = None
        if parts.scheme == "wss":
            pin = await pin_certificate(
                parts.hostname or "",
                parts.port or 443,
                configured=self.profile.tls_fingerprint,
                pin_file=self._pin_file,
                timeout=self._open_timeout,
            )
        transport = await PinnedWebSocketTransport.connect(
            endpoint, headers=self._auth_headers(), pin=pin, open_timeout=self._open_timeout
        )
        conn = connect_to_agent(_AcpClientSide(self), transport)
        try:
            init = await asyncio.wait_for(
                conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
                        terminal=False,
                    ),
                    client_info=Implementation(name=self._client_name, version=__version__),
                ),
                timeout=self._request_timeout,
            )
        except BaseException as exc:
            # Includes cancellation: the socket and the receive task must not leak.
            with contextlib.suppress(Exception):
                await conn.close()
            await transport.close()
            if isinstance(exc, ValidationError):
                reason = "invalid initialize response"
            elif isinstance(exc, ConnectionError | TimeoutError | RequestError):
                reason = str(exc) or type(exc).__name__
            else:
                raise
            raise AgentUnavailable(
                f"ACP handshake with {self.profile.display_name} failed: {reason}"
            ) from exc

        self._conn, self._transport, self.tls_pin = conn, transport, pin
        self._attached.clear()
        self._uncertain.clear()
        self.agent_capabilities = init.agent_capabilities
        self.agent_info = init.agent_info.model_dump(exclude_none=True) if init.agent_info else None
        watcher = asyncio.create_task(self._watch(transport))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        self._log.info(
            "agent connected",
            endpoint=endpoint,
            agent_info=self.agent_info,
            tls=pin.source if pin else "none",
        )

    def _auth_headers(self) -> dict[str, str]:
        if self._secret is None:
            return {}
        value = self._secret.get_secret_value()
        if self.profile.kind == "goose":
            return {"X-Secret-Key": value}
        return {"Authorization": f"Bearer {value}"}

    async def _watch(self, transport: PinnedWebSocketTransport) -> None:
        await transport.closed.wait()
        if self._transport is transport:
            self._attached.clear()
            self._log.warning("agent connection closed")

    async def close(self) -> None:
        for tasks in self._permission_tasks.values():
            for task in tasks:
                task.cancel()
        # Detach first so the watcher does not report a deliberate close as a drop.
        conn, self._conn = self._conn, None
        transport, self._transport = self._transport, None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()
        if transport is not None:
            await transport.close()
        self._attached.clear()

    async def _abandon(self, transport: PinnedWebSocketTransport | None) -> None:
        """Close a connection whose ACP side failed so the next call reconnects.

        The SDK stops reading on some errors while the socket stays open; without
        this the client would look connected and fail every call.
        """
        if transport is None or transport is not self._transport or transport.closed.is_set():
            return
        conn = self._conn
        await transport.close()
        with contextlib.suppress(Exception):
            await conn.close()

    async def _release_quarantine(self, session_id: str) -> None:
        """Reconnect to unblock a session whose cancellation was never confirmed.

        Only a fresh connection clears the quarantine. It is made when nothing else
        runs on this connection, so other sessions' turns are never cut off.
        """
        async with self._connect_lock:
            busy = self._turns or self._loading or self._requests or self._permission_tasks
            if session_id not in self._uncertain or busy:
                return
            self._log.info("reconnecting to release a cancelled session", session_id=session_id)
            await self.close()
        await self.ensure_connected()

    # ---------------------------------------------------------------- sessions

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        """Wire the owner's handler before starting any turns or permission requests."""
        if self._turns or self._loading or any(self._permission_tasks.values()):
            raise SessionBusy("cannot replace the permission handler while the agent is busy")
        self._permission_handler = handler

    async def new_session(self, cwd: str | None = None) -> str:
        await self.ensure_connected()
        cwd = cwd or self.profile.default_cwd
        response = await self._call(self._conn.new_session(cwd=cwd, mcp_servers=[]))
        session_id = response.session_id
        await self._apply_mode(session_id, response.modes)
        self._attached.add(session_id)
        self._session_cwds[session_id] = cwd
        self._log.info("session created", session_id=session_id)
        return session_id

    async def load_session(self, session_id: str, cwd: str | None = None) -> None:
        """Attach an existing session to this connection; its history replay is dropped."""
        await self.ensure_connected()
        if session_id in self._uncertain:
            await self._release_quarantine(session_id)
        if session_id in self._uncertain:
            raise SessionBusy(
                f"session {session_id} has an unconfirmed cancellation; reconnect first"
            )
        self._attached.discard(session_id)
        cwd = cwd or self._session_cwds.get(session_id) or self.profile.default_cwd
        caps = self.agent_capabilities
        if not (caps and caps.load_session):
            raise SessionNotFound(f"{self.profile.display_name} cannot restore sessions")
        self._loading.add(session_id)
        try:
            response = await self._call(
                self._conn.load_session(cwd=cwd, session_id=session_id, mcp_servers=[]),
                not_found=f"session {session_id} not found",
            )
            # Replayed updates were scheduled before the response; let them drain.
            await asyncio.sleep(0)
        finally:
            self._loading.discard(session_id)
        await self._apply_mode(session_id, response.modes if response else None)
        self._attached.add(session_id)
        self._session_cwds[session_id] = cwd
        self._log.info("session loaded", session_id=session_id)

    async def list_sessions(self, cwd: str | None = None) -> list[dict[str, Any]]:
        await self.ensure_connected()
        response = await self._call(self._conn.list_sessions(cwd=cwd))
        return [s.model_dump(by_alias=True, exclude_none=True) for s in response.sessions]

    async def _apply_mode(self, session_id: str, modes: SessionModeState | None) -> None:
        mode = self.profile.session_mode
        if not mode or (modes is not None and modes.current_mode_id == mode):
            return
        available = [m.id for m in modes.available_modes] if modes else []
        if mode not in available:
            self._attached.discard(session_id)
            raise ModeNotAvailable(
                f"{self.profile.display_name} has no session mode {mode!r} (offers {available})"
            )
        await self._call(self._conn.set_session_mode(session_id=session_id, mode_id=mode))

    # ------------------------------------------------------------------- turns

    async def prompt(
        self, session_id: str, text: str, *, cwd: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn; yields events and always ends with :class:`TurnFinished`.

        Closing the iterator before the end cancels the turn on the agent.
        """
        await self.ensure_connected()
        if session_id in self._uncertain and session_id not in self._turns:
            await self._release_quarantine(session_id)
        if session_id in self._turns or session_id in self._uncertain:
            raise SessionBusy(f"session {session_id} is busy or its cancellation is unconfirmed")
        if session_id not in self._attached:
            await self.load_session(session_id, cwd)
        if session_id in self._turns:
            raise SessionBusy(f"session {session_id} is busy")

        queue: asyncio.Queue[AgentEvent | _TurnDone] = asyncio.Queue()
        self._turns[session_id] = queue
        transport = self._transport
        request = asyncio.ensure_future(
            self._conn.prompt(
                session_id=session_id, prompt=[TextContentBlock(type="text", text=text)]
            )
        )
        # Updates sent before the response are processed before this callback runs.
        request.add_done_callback(lambda _: queue.put_nowait(_TurnDone()))
        finished = False
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _TurnDone):
                    break
                yield item
            response = await self._result(request, transport)
            finished = True
            self._log.info("turn finished", session_id=session_id, stop_reason=response.stop_reason)
            yield TurnFinished(
                session_id,
                stop_reason=str(response.stop_reason),
                usage=response.usage.model_dump(by_alias=True, exclude_none=True)
                if response.usage
                else None,
            )
        finally:
            try:
                if not finished and not request.done():
                    self._uncertain.add(session_id)
                    with contextlib.suppress(Exception):
                        await self.cancel(session_id)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            asyncio.shield(request), timeout=DEFAULT_CANCEL_TIMEOUT
                        )
            finally:
                if not request.done() or request.cancelled():
                    # A local task cancellation doesn't stop work on the agent.
                    # Keep this session blocked until a fresh connection is made.
                    self._uncertain.add(session_id)
                    self._attached.discard(session_id)
                    request.cancel()
                else:
                    self._uncertain.discard(session_id)
                with contextlib.suppress(BaseException):
                    await request
                self._turns.pop(session_id, None)

    async def ask(self, session_id: str, text: str) -> AgentReply:
        """Run one turn and return the collected answer."""
        parts: list[str] = []
        finished: TurnFinished | None = None
        async for event in self.prompt(session_id, text):
            if isinstance(event, MessageChunk):
                parts.append(event.text)
            elif isinstance(event, TurnFinished):
                finished = event
        if finished is None:  # prompt() always ends with TurnFinished unless it raised
            raise PromptFailed("the turn ended without a result")
        return AgentReply(session_id, "".join(parts), finished.stop_reason, finished.usage)

    async def cancel(self, session_id: str) -> None:
        """Cancel the running turn; pending permission requests are answered "cancelled"."""
        if self.connected:
            with contextlib.suppress(ConnectionError):
                await self._conn.cancel(session_id=session_id)
        for task in self._permission_tasks.pop(session_id, set()):
            task.cancel()
        self._log.info("turn cancel requested", session_id=session_id)

    # ---------------------------------------------------------- agent → client

    def _on_update(self, session_id: str, update: Any) -> None:
        # Must not await before enqueueing: notification order is task creation order.
        if session_id in self._loading:
            return
        queue = self._turns.get(session_id)
        if queue is not None:
            queue.put_nowait(normalize(session_id, update))

    async def _on_permission(
        self, session_id: str, tool_call: Any, options: list[Any]
    ) -> RequestPermissionResponse:
        if session_id in self._uncertain:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        request = PermissionRequest(
            session_id=session_id,
            tool_call_id=tool_call.tool_call_id,
            title=tool_call.title,
            kind=tool_call.kind,
            raw_input=tool_call.raw_input,
            options=tuple(PermissionOption(o.option_id, o.name, o.kind) for o in options),
        )
        self._log.info("permission requested", session_id=session_id, title=request.title)
        task = asyncio.ensure_future(self._permission_handler(request))
        self._permission_tasks[session_id].add(task)
        try:
            option_id = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                task.cancel()
                raise
            option_id = None  # the turn was cancelled
        except Exception:
            self._log.exception("permission handler failed; rejecting")
            option_id = (
                reject_option.option_id
                if (reject_option := request.option("reject_once"))
                else None
            )
        finally:
            tasks = self._permission_tasks.get(session_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    del self._permission_tasks[session_id]

        if option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if all(o.option_id != option_id for o in request.options):
            self._log.warning(
                "permission handler chose an unknown option; rejecting", option=option_id
            )
            fallback = request.option("reject_once")
            if fallback is None:
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            option_id = fallback.option_id
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=option_id, outcome="selected")
        )

    # ----------------------------------------------------------------- helpers

    async def _call(self, coro: Coroutine[Any, Any, T], *, not_found: str | None = None) -> T:
        transport = self._transport  # the one ``coro`` was created on
        self._requests += 1
        try:
            return await asyncio.wait_for(coro, timeout=self._request_timeout)
        except ConnectionError as exc:
            await self._abandon(transport)
            raise TransportDisconnected(f"lost connection to {self.profile.display_name}") from exc
        except TimeoutError as exc:
            raise AgentUnavailable(f"{self.profile.display_name} did not answer in time") from exc
        except RequestError as exc:
            if not_found is not None:
                raise SessionNotFound(not_found) from exc
            raise AgentError(f"{self.profile.display_name} error: {exc}") from exc
        except ValidationError as exc:
            raise AgentError(f"{self.profile.display_name} sent an invalid response") from exc
        finally:
            self._requests -= 1

    async def _result(
        self, request: asyncio.Future[Any], transport: PinnedWebSocketTransport | None
    ) -> Any:
        try:
            return request.result()
        except ConnectionError as exc:
            await self._abandon(transport)
            raise TransportDisconnected(
                f"lost connection to {self.profile.display_name} during the turn"
            ) from exc
        except RequestError as exc:
            raise PromptFailed(f"{self.profile.display_name} failed the prompt: {exc}") from exc
        except ValidationError as exc:
            raise PromptFailed(
                f"{self.profile.display_name} sent an invalid prompt result"
            ) from exc
