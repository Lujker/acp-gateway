"""Dedicated conversations in an existing Hermes backend (not yet an ACP transport).

Only conversations created by this connection are writable. This local registry
is not an upstream writer lease: the owner must reserve these conversations for
the dispatcher. Hermes 0.21.6 cannot safely correlate concurrent Desktop turns.
No import, cold resume, reconnect/replay, session interrupt or automatic allow.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from acp_gateway.adapters.hermes_models import (
    HermesModelControl,
    HermesModelRefused,
    model_identifier,
)
from acp_gateway.discovery.hermes import HermesDiscoveryError, HermesEndpoint
from acp_gateway.discovery.hermes_rpc import HermesRpcClient, HermesRpcError, stored_session_id

MAX_CONVERSATIONS = 32
SessionState = Literal["idle", "running", "configuring", "uncertain"]


@dataclass(frozen=True)
class HermesConversation:
    handle: str
    runtime_id: str
    stored_id: str
    profile: str
    cwd: str


@dataclass
class _Entry:
    conversation: HermesConversation
    state: SessionState = "idle"
    submitting: bool = False
    models: HermesModelControl = field(default_factory=HermesModelControl)


class _DedicatedRpc(HermesRpcClient):
    """Internal wire policy, independent of diagnostic grants."""

    owner: HermesDedicatedClient

    def _authorize(self, method: str, params: dict) -> None:
        owner = self.owner
        if method == "client.capabilities" and params == {"server_requests": owner.approvals}:
            return
        if method == "session.create" and params == owner._creating:
            return
        entry = owner._runtime.get(params.get("session_id"))
        if (
            entry is not None
            and entry.models.busy
            and entry.models.authorization == (method, params)
        ):
            return
        if method == "prompt.submit" and set(params) == {"profile", "session_id", "text"}:
            entry = owner._runtime.get(params["session_id"])
            if (
                entry is not None
                and entry.submitting
                and params["profile"] == entry.conversation.profile
            ):
                return
        raise HermesDiscoveryError("operation is outside dedicated Hermes access")

    async def _deny(self, frame: dict) -> None:
        params = frame.get("params")
        if not isinstance(params, dict):
            raise ValueError
        entry = self.owner._runtime.get(params.get("session_id"))
        if (
            entry is None
            or entry.state != "running"
            or params.get("profile", entry.conversation.profile) != entry.conversation.profile
        ):
            self.ignored_request_count += 1
            return
        if frame["method"] == "approval" and self.owner.approvals:
            choices = params.get("choices")
            if not isinstance(choices, list):
                raise ValueError
            allowed = tuple(c for c in ("once", "deny") if c in choices)
            if "deny" not in allowed:
                raise ValueError
            rid = frame["id"]
            if rid in self.owner._permissions or len(self.owner._permissions) >= 32:
                raise ValueError
            self.owner._permissions[rid] = (entry, allowed)
            self.owner.events.put_nowait(
                {
                    "handle": entry.conversation.handle,
                    "type": "approval.request",
                    "payload": {
                        "id": rid,
                        "choices": allowed,
                        "command": params.get("command", ""),
                        "description": params.get("description", ""),
                    },
                }
            )
            return
        # Direct driver access fails closed unless the ACP bridge opts in above.
        # Never settle another Desktop chat's requests.
        response = (
            {"result": {"choice": "deny"}}
            if frame["method"] == "approval"
            else {"error": {"code": -32601, "message": "Unsupported dispatcher request"}}
        )
        await self._send({"jsonrpc": "2.0", "id": frame["id"], **response})


class HermesDedicatedClient:
    """Connection-local ownership and bounded events for dispatcher-created chats.

    ``cwd`` is native to the agent host. Root/mode policy belongs to the future
    ACP bridge; this class does not claim to sandbox the Hermes process.
    Losing the connection invalidates all handles. Close detaches only: it does
    not terminate the external backend, delete conversations or cancel work.
    """

    def __init__(self, rpc: _DedicatedRpc, *, profile: str, approvals: bool = False):
        self._rpc = rpc
        rpc.owner = self
        self.profile = profile
        self.approvals = approvals
        self.closed = asyncio.Event()
        self.failure: str | None = None
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)
        self._handles: dict[str, _Entry] = {}
        self._runtime: dict[str, _Entry] = {}
        self._creating: dict | None = None
        self._permissions: dict[str | int, tuple[_Entry, tuple[str, ...]]] = {}
        self._create_lock = asyncio.Lock()
        self._pump = asyncio.create_task(self._read_events())

    @classmethod
    async def connect(
        cls,
        endpoint: HermesEndpoint,
        *,
        profile: str = "default",
        timeout: float = 5,
        approvals: bool = False,
    ) -> HermesDedicatedClient:
        if not isinstance(profile, str) or profile not in endpoint.profiles:
            raise HermesDiscoveryError("Hermes profile is not in the discovered host identity")
        rpc = await _DedicatedRpc.connect(endpoint, timeout=timeout)
        client = cls(rpc, profile=profile, approvals=approvals)
        try:
            await rpc.call("client.capabilities", {"server_requests": approvals})
        except BaseException:
            await client.close()
            raise
        return client

    def _available(self) -> None:
        if self.closed.is_set() or self._rpc.closed.is_set():
            raise HermesDiscoveryError("dedicated Hermes connection is closed; handles are invalid")

    def _entry(self, handle: str) -> _Entry:
        self._available()
        if not isinstance(handle, str) or handle not in self._handles:
            raise HermesDiscoveryError("conversation is not owned by this Hermes connection")
        return self._handles[handle]

    def state(self, handle: str) -> SessionState:
        entry = self._entry(handle)
        return "configuring" if entry.state == "idle" and entry.models.busy else entry.state

    def model_activity(self, handle: str) -> bool:
        """Pending/deferred changes are not an idle proof for package maintenance."""
        models = self._entry(handle).models
        return models.busy or models.pending is not None

    def forget(self, handle: str) -> None:
        """Release an idle local attachment, without deleting or resuming upstream."""
        entry = self._entry(handle)
        if (
            self.state(handle) != "idle"
            or entry.submitting
            or self.model_activity(handle)
            or any(owner is entry for owner, _ in self._permissions.values())
        ):
            raise HermesDiscoveryError("Hermes handle is busy, pending or uncertain")
        entry.models.invalidate_confirmation()
        del self._handles[handle]
        self._runtime.pop(entry.conversation.runtime_id, None)

    def invalidate_model_confirmation(self, handle: str) -> None:
        self._entry(handle).models.invalidate_confirmation()

    async def _model_call(self, entry: _Entry, method: str, params: dict) -> dict:
        entry.models.authorization = (method, params.copy())
        try:
            return await self._rpc.call(method, params)
        finally:
            entry.models.authorization = None

    def _model_entry(self, handle: str) -> _Entry:
        entry = self._entry(handle)
        if entry.state != "idle" or entry.submitting or entry.models.busy:
            raise HermesModelRefused("Hermes conversation is busy or uncertain")
        return entry

    async def model_options(self, handle: str) -> dict:
        entry = self._model_entry(handle)
        entry.models.busy = True
        try:
            payload = await self._model_call(
                entry,
                "model.options",
                {
                    "profile": entry.conversation.profile,
                    "session_id": entry.conversation.runtime_id,
                    "explicit_only": True,
                    "include_unconfigured": False,
                    "refresh": False,
                },
            )
            if entry.state != "idle":
                raise HermesDiscoveryError(
                    "Hermes conversation ownership changed during observation"
                )
            return entry.models.parse_catalogue(payload)
        finally:
            entry.models.busy = False

    async def set_model(
        self, handle: str, *, provider: str, model: str, confirmation_token: str | None = None
    ) -> dict:
        try:
            provider, model = model_identifier(provider), model_identifier(model)
        except HermesDiscoveryError:
            raise HermesModelRefused("invalid Hermes provider or model identifier") from None
        entry = self._model_entry(handle)
        if entry.models.pending is not None:
            raise HermesModelRefused("Hermes model change is pending; no automatic retry")
        confirmed = entry.models.consume_confirmation(provider, model, confirmation_token)
        entry.models.busy = True
        try:
            result = await self._model_call(
                entry,
                "config.set",
                {
                    "profile": entry.conversation.profile,
                    "session_id": entry.conversation.runtime_id,
                    "key": "model",
                    "value": f"{model} --provider {provider} --session",
                    "confirm_expensive_model": confirmed,
                },
            )
            if entry.state != "idle":
                raise HermesDiscoveryError("Hermes conversation ownership changed during selection")
            return entry.models.accept_response(result, provider, model)
        except BaseException as exc:
            # Even an RPC error can follow partial application. Do not infer rollback
            # or repeat configuration writes after a timeout/cancellation/invalid ack.
            if isinstance(exc, HermesRpcError) and exc.code == -32601:
                # JSON-RPC method-not-found explicitly means no handler executed.
                raise
            self._quarantine(entry, "model_selection_outcome_unknown")
            raise
        finally:
            entry.models.busy = False

    def _quarantine(self, entry: _Entry, reason: str) -> None:
        if entry.state == "uncertain":
            return
        entry.state = "uncertain"
        self._clear_permissions(entry)
        self.events.put_nowait(
            {
                "handle": entry.conversation.handle,
                "type": "gateway.uncertain",
                "payload": {"reason": reason},
            }
        )

    def _clear_permissions(self, entry: _Entry) -> None:
        for rid, (owner, _) in list(self._permissions.items()):
            if owner is entry:
                del self._permissions[rid]

    async def respond_approval(self, handle: str, request_id: str | int, choice: str) -> bool:
        """Forward one decision, never claiming upstream settlement/acceptance.

        False means withdrawn/already answered. No persistent grants are allowed.
        A successful send is not proof that we won Hermes' first-response race.
        """
        entry = self._entry(handle)
        pending = self._permissions.get(request_id)
        if pending is None:
            return False
        if pending[0] is not entry or choice not in pending[1]:
            raise HermesDiscoveryError("approval decision is outside dedicated Hermes access")
        del self._permissions[request_id]
        if entry.state != "running":
            return False
        try:
            await self._rpc._send(
                {"jsonrpc": "2.0", "id": request_id, "result": {"choice": choice}}
            )
        except BaseException:
            self._quarantine(entry, "approval_delivery_unknown")
            raise
        return True

    async def create(self, *, cwd: str) -> HermesConversation:
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            raise HermesDiscoveryError("a native Hermes working directory is required")
        async with self._create_lock:
            self._available()
            if len(self._handles) >= MAX_CONVERSATIONS:
                raise HermesDiscoveryError("dedicated Hermes conversation limit reached")
            self._creating = {
                "profile": self.profile,
                "source": "acp_gateway",
                "title": "Dispatcher",
                "cwd": cwd,
                "cwd_explicit": True,
                "close_on_disconnect": False,
            }
            try:
                snapshot = await self._rpc.call("session.create", self._creating)
                runtime_id = snapshot.get("session_id")
                stored_id = stored_session_id(snapshot)
                if (
                    not isinstance(runtime_id, str)
                    or not runtime_id
                    or not stored_id
                    or runtime_id in self._runtime
                    or any(e.conversation.stored_id == stored_id for e in self._handles.values())
                ):
                    raise HermesDiscoveryError("invalid or reused dedicated Hermes identity")
                conversation = HermesConversation(
                    uuid4().hex, runtime_id, stored_id, self.profile, cwd
                )
                entry = _Entry(conversation)
                self._handles[conversation.handle] = entry
                self._runtime[runtime_id] = entry
                return conversation
            except BaseException:
                # Create may have executed. Do not discover/adopt a chat by title
                # or repeat the call after an unknown outcome.
                await self.close()
                raise
            finally:
                self._creating = None

    async def submit(self, handle: str, text: str) -> None:
        entry = self._entry(handle)
        if not isinstance(text, str) or not text.strip():
            raise HermesDiscoveryError("a nonempty Hermes prompt is required")
        if entry.state != "idle" or entry.submitting or entry.models.busy:
            raise HermesDiscoveryError("dedicated Hermes conversation is busy or uncertain")
        entry.models.invalidate_confirmation()
        entry.state = "running"
        entry.submitting = True
        try:
            result = await self._rpc.call(
                "prompt.submit",
                {
                    "profile": entry.conversation.profile,
                    "session_id": entry.conversation.runtime_id,
                    "text": text,
                },
            )
            if result.get("status") != "streaming":
                # queued/steered/redirected means the reserved-chat assumption
                # did not hold. No safe automatic interrupt or replay exists.
                raise HermesDiscoveryError("Hermes did not accept an isolated dispatcher turn")
        except BaseException:
            entry.state = "uncertain"
            self._clear_permissions(entry)
            raise
        finally:
            entry.submitting = False

    async def _read_events(self) -> None:
        read = asyncio.create_task(self._rpc.events.get())
        closed = asyncio.create_task(self._rpc.closed.wait())
        try:
            while True:
                done, _ = await asyncio.wait({read, closed}, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    break
                frame = read.result()
                read = asyncio.create_task(self._rpc.events.get())
                if frame.get("method") != "event":
                    continue
                params = frame.get("params")
                if not isinstance(params, dict):
                    raise ValueError
                entry = self._runtime.get(params.get("session_id"))
                if entry is None:
                    continue
                if params.get("profile", self.profile) != self.profile:
                    self._quarantine(entry, "profile_mismatch")
                    continue
                kind = params.get("type")
                payload = params.get("payload")
                if not isinstance(kind, str) or (
                    payload is not None and not isinstance(payload, dict)
                ):
                    raise ValueError
                if kind == "session.info" and not entry.models.busy:
                    entry.models.invalidate_confirmation()
                if kind == "request.cancel":
                    rid = (payload or {}).get("id")
                    pending = self._permissions.get(rid)
                    if pending is None or pending[0] is not entry:
                        continue
                    del self._permissions[rid]
                if kind in {"message.start", "message.delta", "message.complete"}:
                    if entry.state != "running":
                        self._quarantine(entry, "unexpected_turn")
                        continue
                    if kind == "message.complete":
                        self._clear_permissions(entry)
                        status = (payload or {}).get("status")
                        if status in {"complete", "error"}:
                            entry.state = "idle"
                            if entry.models.pending is not None:
                                entry.models.turn_completed = True
                        else:
                            self._quarantine(entry, "unproven_completion")
                if entry.state != "uncertain":
                    self.events.put_nowait(
                        {"handle": entry.conversation.handle, "type": kind, "payload": payload}
                    )
        except Exception:
            self.failure = "event_protocol_or_capacity_failure"
        finally:
            for task in (read, closed):
                task.cancel()
            await asyncio.gather(read, closed, return_exceptions=True)
            self.closed.set()
            self._permissions.clear()
            for entry in self._handles.values():
                entry.state = "uncertain"
            await self._rpc.close()

    async def close(self) -> None:
        self._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pump
        # The pump may have been cancelled before its first scheduling point.
        self.closed.set()
        self._permissions.clear()
        for entry in self._handles.values():
            entry.state = "uncertain"
        await self._rpc.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
