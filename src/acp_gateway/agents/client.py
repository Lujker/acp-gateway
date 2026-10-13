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
    ModelRefused,
    ModeNotAvailable,
    PromptFailed,
    SessionBusy,
    SessionNotFound,
    TLSFingerprintMismatch,
    TransportDisconnected,
)
from acp_gateway.agents.events import AgentEvent, MessageChunk, TurnFinished, normalize
from acp_gateway.agents.model_control import (
    MODEL_OPTIONS,
    MODEL_RECOVER,
    MODEL_REFUSED,
    MODEL_SET,
    model_params,
    model_result,
)
from acp_gateway.agents.tls import TlsPin, pin_certificate
from acp_gateway.agents.transport import (
    AgentTransport,
    PinnedWebSocketTransport,
    TransportFactory,
)
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

    def __init__(self, owner: AgentClient, transport: AgentTransport) -> None:
        self._owner = owner
        self._transport = transport

    def _current(self) -> bool:
        return self._owner._transport is self._transport and not self._transport.closed.is_set()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if self._current():
            self._owner._on_update(session_id, update)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> RequestPermissionResponse:
        if not self._current():
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        response = await self._owner._on_permission(session_id, tool_call, options)
        if not self._current():
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return response

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
        transport_factory: TransportFactory | None = None,
    ) -> None:
        if profile.backend == "connector" and transport_factory is None:
            raise ValueError("connector agent requires a relay transport factory")
        self.profile = profile
        self._secret = secret
        self._pin_file = pin_dir / f"{profile.alias}.sha256"
        self._permission_handler = permission_handler
        self._open_timeout = open_timeout
        self._request_timeout = request_timeout
        self._reconnect_delays = tuple(reconnect_delays)
        self._client_name = client_name
        self._transport_factory = transport_factory
        self._log = get_logger(__name__).bind(agent=profile.address)

        self._conn: Any = None
        self._transport: AgentTransport | None = None
        self._connect_lock = asyncio.Lock()
        self._attached: set[str] = set()  # sessions usable on the current connection
        self._uncertain: set[str] = set()  # cancellation not confirmed on this connection
        self._bindings: dict[str, dict] = {}
        self._session_cwds: dict[str, str] = {}
        self._loading: set[str] = set()  # sessions whose history replay is suppressed
        self._turns: dict[str, asyncio.Queue[AgentEvent | _TurnDone]] = {}
        self._permission_tasks: defaultdict[str, set[asyncio.Task[str | None]]] = defaultdict(set)
        self._permission_tools: dict[tuple[str, str], asyncio.Task] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._requests = 0  # requests in flight outside prompt turns
        self._model_operations: set[str] = set()
        self._model_uncertain: set[str] = set()

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
        transport, pin = await self.open_transport()
        conn = None
        try:
            conn = connect_to_agent(_AcpClientSide(self, transport), transport)
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
            if conn is not None:
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
            endpoint=(
                "local-hermes"
                if self.profile.backend == "hermes"
                else self.profile.acp_endpoint
                if self._transport_factory is None
                else "injected"
            ),
            agent_info=self.agent_info,
            tls=pin.source if pin else ("transport-managed" if self._transport_factory else "none"),
        )

    async def open_transport(self) -> tuple[AgentTransport, TlsPin | None]:
        """Open one stream; injected factories own authentication and endpoint policy."""
        if self._transport_factory is not None:
            return await self._transport_factory(), None
        if self.profile.backend == "hermes":
            from acp_gateway.adapters.hermes import HermesDedicatedClient
            from acp_gateway.adapters.hermes_acp import HermesAcpTransport
            from acp_gateway.connectors.policy import LocalAgentPolicy
            from acp_gateway.discovery.hermes import (
                HermesDiscoveryError,
                default_lock_dir,
                read_endpoint,
            )

            settings = self.profile.hermes
            policy = LocalAgentPolicy.from_profile(self.profile)
            try:
                endpoint = await asyncio.to_thread(
                    read_endpoint, settings.lock_dir or default_lock_dir(), settings.role
                )
                driver = await HermesDedicatedClient.connect(
                    endpoint, profile=settings.profile, timeout=self._open_timeout, approvals=True
                )
            except (HermesDiscoveryError, OSError):
                raise AgentUnavailable(
                    "local Hermes discovery or verified connection failed"
                ) from None
            return HermesAcpTransport(driver, policy), None
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
        return transport, pin

    def _auth_headers(self) -> dict[str, str]:
        if self._secret is None:
            return {}
        value = self._secret.get_secret_value()
        if self.profile.kind == "goose":
            return {"X-Secret-Key": value}
        return {"Authorization": f"Bearer {value}"}

    async def _watch(self, transport: AgentTransport) -> None:
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

    async def _abandon(self, transport: AgentTransport | None) -> None:
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
        from acp_gateway.agents.bindings import session_binding

        try:
            metadata = (getattr(response, "field_meta", None) or {}).get("acp-gateway") or {}
            binding = session_binding(metadata.get("sessionBinding"))
        except (ValueError, AttributeError):
            raise AgentError("invalid agent session binding; no automatic retry") from None
        if binding is not None:
            self._bindings[session_id] = binding
        await self._apply_mode(session_id, response.modes)
        self._attached.add(session_id)
        self._session_cwds[session_id] = cwd
        self._log.info("session created", session_id=session_id)
        return session_id

    def session_binding(self, session_id: str) -> dict | None:
        binding = self._bindings.get(session_id)
        return dict(binding) if binding is not None else None

    def remember_session_binding(self, session_id: str, binding) -> None:
        """Restore provenance only; the connector must still authorize live load."""
        from acp_gateway.agents.bindings import session_binding

        validated = session_binding(binding)
        if validated is not None:
            existing = self._bindings.get(session_id)
            if existing is not None and existing != validated:
                raise SessionNotFound("Hermes dialogue binding changed")
            self._bindings[session_id] = validated

    def session_availability(self, session_id: str) -> str:
        connected = self._transport is not None and not self._transport.closed.is_set()
        if (
            session_id in self._model_uncertain
            or session_id in self._uncertain
            or (session_id in self._turns and not connected)
        ):
            return "uncertain"
        if session_id in self._turns or session_id in self._model_operations:
            return "busy"
        if connected and session_id in self._attached:
            return "attached"
        if self._bindings.get(session_id, {}).get("restore") == "connector-live":
            return "restore-required"
        if (
            self.profile.kind == "hermes"
            or self._bindings.get(session_id, {}).get("restore") == "disabled"
        ):
            return "restore-unavailable"
        return "restore-required"

    async def load_session(self, session_id: str, cwd: str | None = None) -> None:
        """Attach an existing session to this connection; its history replay is dropped."""
        if session_id in self._model_uncertain:
            raise SessionBusy("Model selection outcome is unknown; no automatic retry")
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
        binding = self._bindings.get(session_id)
        if binding and binding.get("restore") == "connector-live":
            metadata = getattr(caps, "field_meta", None) or {}
            if (metadata.get("acp-gateway") or {}).get("connectorLiveResume") is not True:
                raise SessionNotFound("Connector cannot restore this live Hermes dialogue")
        self._loading.add(session_id)
        try:
            response = await self._call(
                self._conn.load_session(cwd=cwd, session_id=session_id, mcp_servers=[]),
                not_found=(
                    "The live Hermes dialogue is unavailable, busy or uncertain. "
                    "Your selected dialogue is unchanged; use /new only to create a different one."
                    if binding and binding.get("restore") == "connector-live"
                    else f"session {session_id} not found"
                ),
            )
            # Replayed updates were scheduled before the response; let them drain.
            await asyncio.sleep(0)
        finally:
            self._loading.discard(session_id)
        if binding and binding.get("restore") == "connector-live":
            metadata = getattr(response, "field_meta", None) or {}
            actual = (metadata.get("acp-gateway") or {}).get("sessionBinding")
            if actual != binding:
                raise SessionNotFound("Connector Hermes dialogue identity did not match")
        await self._apply_mode(session_id, response.modes if response else None)
        self._attached.add(session_id)
        self._session_cwds[session_id] = cwd
        self._log.info("session loaded", session_id=session_id)

    async def list_sessions(self, cwd: str | None = None) -> list[dict[str, Any]]:
        await self.ensure_connected()
        response = await self._call(self._conn.list_sessions(cwd=cwd))
        return [s.model_dump(by_alias=True, exclude_none=True) for s in response.sessions]

    @property
    def supports_model_control(self) -> bool:
        caps = self.agent_capabilities
        meta = getattr(caps, "field_meta", None)
        flags = meta.get("acp-gateway") if isinstance(meta, dict) else None
        return (
            isinstance(flags, dict)
            and type(flags.get("modelControl")) is int
            and flags["modelControl"] == 1
        )

    async def model_options(
        self, session_id: str, *, admission: Callable[[], None] | None = None
    ) -> dict:
        return await self._model_request(
            MODEL_OPTIONS, {"sessionId": session_id}, admission=admission
        )

    async def set_model(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
        confirmation_token: str | None = None,
        operation_id: str | None = None,
        admission: Callable[[], None] | None = None,
    ) -> dict:
        params = {"sessionId": session_id, "provider": provider, "model": model}
        if confirmation_token is not None:
            params["confirmationToken"] = confirmation_token
        if operation_id is not None:
            params["operationId"] = operation_id
        return await self._model_request(MODEL_SET, params, admission=admission)

    async def recover_model(self, session_id: str, operation_id: str, *, admission=None) -> dict:
        """Read one exact retained model outcome; never resend the setter."""
        if session_id in self._turns or session_id in self._model_operations:
            raise SessionBusy("Dialogue already has an active operation")
        binding = self._bindings.get(session_id)
        try:
            params = model_params(
                MODEL_RECOVER,
                {"sessionId": session_id, "operationId": operation_id, "binding": binding},
            )
        except ValueError:
            return {"status": "unavailable"}
        self._model_operations.add(session_id)
        try:
            await self.ensure_connected()
            meta = getattr(self.agent_capabilities, "field_meta", None) or {}
            if (meta.get("acp-gateway") or {}).get("modelRecovery") != 1:
                return {"status": "unavailable"}
            if admission is not None:
                admission()
            result = model_result(
                MODEL_RECOVER,
                await self._call(self._conn.ext_method(MODEL_RECOVER[1:], params)),
            )
            if result["status"] in {"accepted", "deferred", "not_applied"}:
                self._model_uncertain.discard(session_id)
                self._attached.discard(session_id)
            return result
        finally:
            self._model_operations.discard(session_id)

    async def _model_request(
        self, method: str, params: dict, *, admission: Callable[[], None] | None = None
    ) -> dict:
        try:
            params = model_params(method, params)
        except ValueError:
            raise AgentError("Invalid model operation parameters") from None
        sid = params["sessionId"]
        if sid not in self._attached and sid not in self._bindings:
            raise SessionNotFound("Model control requires a known dispatcher dialogue")
        if sid in self._model_uncertain:
            raise SessionBusy("Model selection outcome is unknown; no automatic retry")
        if (
            sid in self._turns
            or sid in self._model_operations
            or sid in self._uncertain
            or sid in self._loading
        ):
            raise SessionBusy("Dialogue is busy or uncertain")
        self._model_operations.add(sid)
        started = False
        transport = None
        try:
            await self.ensure_connected()
            if not self.supports_model_control:
                raise AgentError("Model control is unavailable for this agent")
            if sid not in self._attached:
                await self.load_session(sid)
            transport = self._transport
            if admission is not None:
                admission()
            started = True
            response = await self._call(self._conn.ext_method(method[1:], params))
            return model_result(method, response)
        except BaseException as exc:
            cause = exc.__cause__
            refused = isinstance(cause, RequestError) and cause.code in {MODEL_REFUSED, -32601}
            if started and not refused:
                if method == MODEL_SET:
                    self._model_uncertain.add(sid)
                    self._attached.discard(sid)
                await self._abandon(transport)
            if refused:
                raise ModelRefused(str(exc)) from cause
            if isinstance(exc, ValueError):
                raise AgentError("Invalid model operation response") from None
            raise
        finally:
            self._model_operations.discard(sid)

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
        if session_id in self._model_operations or session_id in self._model_uncertain:
            raise SessionBusy("Model operation is pending or its outcome is unknown")
        binding = self._bindings.get(session_id, {})
        if binding.get("restore") == "disabled" and self.session_availability(session_id) in {
            "uncertain",
            "restore-unavailable",
        }:
            raise SessionNotFound("Hermes dialogue cannot safely be restored; create a new session")
        await self.ensure_connected()
        if session_id in self._uncertain and session_id not in self._turns:
            await self._release_quarantine(session_id)
        if session_id in self._turns or session_id in self._uncertain:
            raise SessionBusy(f"session {session_id} is busy or its cancellation is unconfirmed")
        if session_id not in self._attached:
            await self.load_session(session_id, cwd)
        if session_id in self._model_operations or session_id in self._model_uncertain:
            raise SessionBusy("Model operation is pending or its outcome is unknown")
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
                elif (
                    not finished and self._bindings.get(session_id, {}).get("restore") == "disabled"
                ):
                    # A failed Hermes result does not prove upstream work stopped.
                    self._uncertain.add(session_id)
                    self._attached.discard(session_id)
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

    @property
    def supports_cancel(self) -> bool:
        caps = self.agent_capabilities
        meta = caps.field_meta if caps else None
        flags = meta.get("acp-gateway") if isinstance(meta, dict) else None
        return not (isinstance(flags, dict) and flags.get("cancel") is False)

    async def cancel(self, session_id: str) -> None:
        """Cancel the running turn; pending permission requests are answered "cancelled"."""
        if not self.supports_cancel:
            raise AgentError("safe turn cancellation is unavailable for this agent")
        if self.connected:
            with contextlib.suppress(ConnectionError):
                await self._conn.cancel(session_id=session_id)
        for task in self._permission_tasks.pop(session_id, set()):
            task.cancel()
        self._log.info("turn cancel requested", session_id=session_id)

    # ---------------------------------------------------------- agent → client

    def _on_update(self, session_id: str, update: Any) -> None:
        # Must not await before enqueueing: notification order is task creation order.
        meta = getattr(update, "field_meta", None)
        flags = meta.get("acp-gateway") if isinstance(meta, dict) else None
        if isinstance(flags, dict) and flags.get("permissionWithdrawn") is True:
            key = (session_id, getattr(update, "tool_call_id", ""))
            if task := self._permission_tools.get(key):
                task.cancel()
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
        tool_key = (session_id, request.tool_call_id)
        self._permission_tools[tool_key] = task
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
            if self._permission_tools.get(tool_key) is task:
                del self._permission_tools[tool_key]
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

    async def _result(self, request: asyncio.Future[Any], transport: AgentTransport | None) -> Any:
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
