"""Connector-owned live Hermes sessions, independent of a remote relay epoch.

Only idle, locally created handles can be rebound. This is a single-owner
connector boundary, not multi-user authorization or upstream cold resume.
Losing a stream during a prompt quarantines its dialogue permanently for this
manager lifetime. No prompt, permission response or result is replayed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import uuid4

from acp import PROTOCOL_VERSION

from acp_gateway.agents.bindings import session_binding
from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.agents.model_control import (
    MODEL_METHODS,
    MODEL_RECOVER,
    MODEL_REFUSED,
    MODEL_SET,
    model_result,
)
from acp_gateway.connectors.policy import LocalPolicyError
from acp_gateway.discovery.hermes import HermesDiscoveryError
from acp_gateway.log import redact_text


@dataclass
class _Session:
    cwd: str
    binding: dict
    owner: ManagedHermesStream | None
    blocked: bool = False
    sequence: int = 0
    journal: deque = field(default_factory=lambda: deque(maxlen=128))
    last_result: dict | None = None
    model_loss: str | None = None
    model_result: dict | None = None


@dataclass
class _Request:
    owner: ManagedHermesStream
    original_id: int | str
    method: str
    params: dict


class HermesSessionManager:
    def __init__(self, factory, policy):
        self.factory, self.policy = factory, policy
        self._engine = None
        self._pump = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._opening = False
        self._lost_authority = False
        self.maintenance_reserved = False
        self.failure = None
        self._initialization = None
        self._sessions: dict[str, _Session] = {}
        self._streams: set[ManagedHermesStream] = set()
        self._requests: dict[str, _Request] = {}
        self._permissions: dict[str, tuple[ManagedHermesStream, str]] = {}

    def _record(self, sid, kind, **fields):
        session = self._sessions.get(sid)
        if session is not None:
            session.sequence += 1
            session.journal.append({"sequence": session.sequence, "kind": kind, **fields})

    def sessions(self):
        return [
            {
                "handle": sid,
                "profile": session.binding["profile"],
                "attached": session.owner is not None,
                "blocked": session.blocked,
                "idle": self.idle(sid),
                "journal_events": len(session.journal),
                "last_result": session.last_result,
                "model_result": session.model_result,
            }
            for sid, session in self._sessions.items()
        ]

    def journal(self, sid):
        session = self._sessions.get(sid)
        if session is None:
            raise ValueError("Unknown retained Hermes handle")
        return {
            "handle": sid,
            "blocked": session.blocked,
            "last_result": session.last_result,
            "model_result": session.model_result,
            "events": list(session.journal),
            "first_sequence": session.journal[0]["sequence"] if session.journal else None,
            "last_sequence": session.sequence,
        }

    def recover_model(self, params, stream):
        sid, operation = params["sessionId"], params["operationId"]
        session = self._sessions.get(sid)
        if (
            session is None
            or session.binding != params["binding"]
            or (session.owner is not None and session.owner is not stream)
        ):
            return {"status": "unavailable"}
        if any(p.params.get("sessionId") == sid for p in self._requests.values()):
            return {"status": "pending"}
        result = session.model_result
        if result is None or result["operation_id"] != operation:
            return {"status": "unavailable"}
        outcome = result["status"]
        if (
            outcome not in {"accepted", "deferred", "not_applied"}
            or self._engine.driver.state(sid) != "idle"
            or (session.blocked and session.model_loss != operation)
        ):
            return {"status": "unknown"}
        # This releases only a model-write loss with an authoritative terminal ACK.
        # Prompt-loss fences and cold/foreign bindings are never released.
        session.blocked = False
        session.model_loss = None
        self._record(sid, "model_reconciled", operation_id=operation, outcome=outcome)
        return {
            "status": outcome,
            **(
                {"resolved_request": result["resolved_request"]}
                if result.get("resolved_request")
                else {}
            ),
        }

    def retire(self, sid):
        session = self._sessions.get(sid)
        if (
            session is None
            or session.owner is not None
            or not self.idle(sid)
            or self._engine.driver.model_activity(sid)
            or any(p.params.get("sessionId") == sid for p in self._requests.values())
            or any(owner_sid == sid for _, owner_sid in self._permissions.values())
        ):
            raise ValueError("Only a detached, idle, unblocked handle can be retired")
        self._engine.forget_session(sid)
        del self._sessions[sid]
        return {"retired": sid, "upstream_deleted": False}

    async def open(self):
        async with self._lock:
            if self.maintenance_reserved:
                raise AgentUnavailable("Connector reserved for maintenance")
            if self._closed:
                raise AgentUnavailable("Hermes session manager is stopped")
            if self._engine is None or self._engine.closed.is_set():
                if self._pump is not None:
                    await self._pump
                if self.maintenance_reserved:
                    raise AgentUnavailable("Connector reserved for maintenance")
                self._opening = True
                try:
                    engine = await self.factory()
                finally:
                    self._opening = False
                self._opening = True
                try:
                    await engine.send(
                        {
                            "jsonrpc": "2.0",
                            "id": "manager-init",
                            "method": "initialize",
                            "params": {"protocolVersion": PROTOCOL_VERSION},
                        }
                    )
                    reply = await asyncio.wait_for(engine.receive(), 5)
                    if reply is None or "result" not in reply:
                        raise AgentUnavailable("Hermes manager initialization failed")
                    initialization = deepcopy(reply["result"])
                    caps = initialization["agentCapabilities"]
                    caps["loadSession"] = True
                    caps["_meta"]["acp-gateway"]["connectorLiveResume"] = True
                    caps["_meta"]["acp-gateway"]["modelRecovery"] = 1
                except BaseException:
                    await engine.close()
                    raise
                finally:
                    self._opening = False
                self._engine = engine
                self._initialization = initialization
                self._pump = asyncio.create_task(self._read(engine))
            if len(self._streams) >= 32:
                raise AgentUnavailable("Hermes managed stream limit reached")
            stream = ManagedHermesStream(self)
            self._streams.add(stream)
            return stream

    async def _read(self, engine):
        try:
            while (frame := await engine.receive()) is not None:
                if "method" not in frame:
                    pending = self._requests.pop(frame.get("id"), None)
                    if pending is None:
                        continue
                    owner = pending.owner
                    result = frame.get("result")
                    if pending.method == "session/new" and isinstance(result, dict):
                        sid = result["sessionId"]
                        binding = session_binding(
                            result["_meta"]["acp-gateway"]["sessionBinding"]
                            | {"restore": "connector-live"}
                        )
                        self._sessions[sid] = _Session(
                            pending.params["cwd"],
                            binding,
                            owner if not owner.closed.is_set() else None,
                        )
                        if not owner.closed.is_set():
                            owner.sessions.add(sid)
                        result = {**result, "_meta": {"acp-gateway": {"sessionBinding": binding}}}
                        frame = {**frame, "result": result}
                        self._record(sid, "created")
                    if pending.method == "session/prompt":
                        sid = pending.params["sessionId"]
                        if "error" in frame:
                            self._sessions[sid].blocked = True
                        self._clear_permissions(sid)
                        outcome = {
                            "request_id": str(pending.original_id)[:128],
                            "outcome": "failed" if "error" in frame else "completed",
                            "stop_reason": (
                                result.get("stopReason")
                                if isinstance(result, dict)
                                and result.get("stopReason")
                                in {
                                    "end_turn",
                                    "max_tokens",
                                    "max_turn_requests",
                                    "refusal",
                                    "cancelled",
                                }
                                else None
                            ),
                        }
                        self._sessions[sid].last_result = outcome
                        self._record(sid, "prompt_result", **outcome)
                    if pending.method == MODEL_SET:
                        sid = pending.params["sessionId"]
                        operation = pending.params.get("operationId")
                        outcome = "unknown"
                        if isinstance(result, dict):
                            projected = model_result(MODEL_SET, result)
                            outcome = projected["status"]
                            if outcome == "confirmation_required":
                                outcome = "not_applied"
                        elif frame.get("error", {}).get("code") in {MODEL_REFUSED, -32601}:
                            outcome = "not_applied"
                        self._sessions[sid].model_result = {
                            "operation_id": operation,
                            "status": outcome,
                            "resolved_request": projected.get("resolved_request")
                            if isinstance(result, dict)
                            else None,
                        }
                        self._record(sid, "model_result", outcome=outcome, operation_id=operation)

                        if owner.closed.is_set():
                            self._engine.driver.invalidate_model_confirmation(sid)
                        if "error" in frame and not self.idle(sid):
                            self._sessions[sid].blocked = True
                    owner.emit({**frame, "id": pending.original_id})
                else:
                    sid = frame.get("params", {}).get("sessionId")
                    session = self._sessions.get(sid)
                    owner = session.owner if session is not None else None
                    if frame["method"] == "session/update":
                        update = frame.get("params", {}).get("update", {})
                        content = update.get("content", {})
                        if (
                            update.get("sessionUpdate") == "agent_message_chunk"
                            and isinstance(content, dict)
                            and content.get("type") == "text"
                            and isinstance(content.get("text"), str)
                        ):
                            self._record(
                                sid, "answer_chunk", text=redact_text(content["text"])[:512]
                            )
                    if owner is None or owner.closed.is_set():
                        continue
                    if frame["method"] == "session/request_permission":
                        self._permissions[frame["id"]] = (owner, sid)
                    if frame["method"] == "session/update":
                        update = frame["params"].get("update", {})
                        meta = update.get("_meta", {}).get("acp-gateway", {})
                        if meta.get("permissionWithdrawn"):
                            self._permissions.pop(update.get("toolCallId"), None)
                    owner.emit(frame)
        except Exception:
            # Protocol errors must not preserve authority or echo backend data.
            self.failure = "Hermes_managed_stream_protocol_failure"
        finally:
            if not self._closed and (self._sessions or self._requests or self._permissions):
                # Clearing a broken attachment must not manufacture an idle proof.
                self._lost_authority = True
            for stream in list(self._streams):
                await stream.close()
            self._sessions.clear()
            self._requests.clear()
            self._permissions.clear()
            await engine.close()

    def _clear_permissions(self, sid):
        for rid, (_, session) in list(self._permissions.items()):
            if session == sid:
                del self._permissions[rid]

    def detach(self, stream):
        for pending in self._requests.values():
            if pending.owner is stream and pending.method in {"session/prompt", MODEL_SET}:
                sid = pending.params["sessionId"]
                self._sessions[sid].blocked = True
                self._sessions[sid].model_loss = (
                    pending.params.get("operationId") if pending.method == MODEL_SET else None
                )
                self._record(sid, "stream_lost", outcome="unknown", method=pending.method)
        for sid in stream.sessions:
            session = self._sessions.get(sid)
            if session is not None and session.owner is stream:
                session.owner = None
                self._clear_permissions(sid)
                if self._engine is not None:
                    with contextlib.suppress(HermesDiscoveryError):
                        self._engine.driver.invalidate_model_confirmation(sid)
        self._streams.discard(stream)

    def idle(self, sid):
        session = self._sessions.get(sid)
        if session is None or session.blocked:
            return False
        try:
            return self._engine.driver.state(sid) == "idle"
        except HermesDiscoveryError:
            return False

    def maintenance_status(self):
        uncertain = sum(session.blocked for session in self._sessions.values())
        running = 0
        for sid, session in self._sessions.items():
            if session.blocked:
                continue
            try:
                state = self._engine.driver.state(sid)
            except (HermesDiscoveryError, AttributeError):
                state = "uncertain"
            if state == "running":
                running += 1
            elif state != "idle":
                uncertain += 1
            elif self._engine.driver.model_activity(sid):
                # Pending compute-host selections need a later owned turn/readback;
                # stopping the connector now would discard their unresolved state.
                uncertain += 1
        return {
            "pending_requests": len(self._requests),
            "pending_approvals": len(self._permissions),
            "running_sessions": running,
            "uncertain_sessions": uncertain + int(self._lost_authority),
            "opening": int(self._opening),
        }

    async def close(self):
        async with self._lock:
            self._closed = True
            for stream in list(self._streams):
                await stream.close()
            if self._engine is not None:
                await self._engine.close()
            if self._pump is not None:
                await self._pump


class ManagedHermesStream:
    def __init__(self, manager):
        self.manager = manager
        self.closed = asyncio.Event()
        self.initialized = False
        self.sessions: set[str] = set()
        self.out = asyncio.Queue(maxsize=128)
        self._buffered_bytes = 0
        self._overflow = None

    def emit(self, frame):
        if self.closed.is_set():
            return
        try:
            size = len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
            if size > 256 * 1024 or self._buffered_bytes + size > 1024 * 1024:
                raise asyncio.QueueFull
            self.out.put_nowait((frame, size))
            self._buffered_bytes += size
        except asyncio.QueueFull:
            # Revoke synchronously before another message can acquire authority.
            self.manager.detach(self)
            self.closed.set()
            self._overflow = asyncio.create_task(self.close())

    def reply(self, rid, result):
        self.emit({"jsonrpc": "2.0", "id": rid, "result": result})

    async def send(self, frame):
        if self.closed.is_set():
            raise ConnectionError("Hermes managed stream is closed")
        manager = self.manager
        try:
            if "method" not in frame:
                permission = manager._permissions.get(frame.get("id"))
                if permission is None or permission[0] is not self:
                    raise LocalPolicyError("permission is not owned by this live stream")
                del manager._permissions[frame["id"]]
                await manager._engine.send(frame)
                return
            clean = manager.policy.prepare(frame)
            method, params = clean["method"], clean["params"]
            rid = clean.get("id")
            if (
                manager.maintenance_reserved
                and method
                in {
                    "session/new",
                    "session/load",
                    "session/prompt",
                }
                | MODEL_METHODS
            ):
                self.emit(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {
                            "code": MODEL_REFUSED if method in MODEL_METHODS else -32000,
                            "message": "Connector reserved for maintenance",
                        },
                    }
                )
                return
            if method == "initialize":
                if self.initialized or params["protocolVersion"] != PROTOCOL_VERSION:
                    raise LocalPolicyError("invalid managed ACP initialization")
                self.initialized = True
                self.reply(rid, deepcopy(manager._initialization))
                return
            if not self.initialized:
                raise LocalPolicyError("initialize before using managed Hermes sessions")
            if method == MODEL_RECOVER:
                if manager.maintenance_reserved:
                    self.reply(rid, {"status": "unavailable"})
                else:
                    self.reply(rid, manager.recover_model(params, self))
                return
            if method == "session/load":
                sid = params["sessionId"]
                session = manager._sessions.get(sid)
                if (
                    session is None
                    or session.cwd != params["cwd"]
                    or not manager.idle(sid)
                    or (session.owner is not None and session.owner is not self)
                ):
                    self.emit(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {
                                "code": -32001,
                                "message": "Live Hermes dialogue unavailable",
                            },
                        }
                    )
                    return
                session.owner = self
                self.sessions.add(sid)
                self.reply(rid, {"_meta": {"acp-gateway": {"sessionBinding": session.binding}}})
                return
            if method == "session/list":
                self.reply(
                    rid,
                    {
                        "sessions": [
                            {
                                "sessionId": sid,
                                "cwd": manager._sessions[sid].cwd,
                                "title": "Dispatcher",
                            }
                            for sid in self.sessions
                            if manager._sessions[sid].cwd == params["cwd"]
                        ]
                    },
                )
                return
            if method in {"session/prompt"} | MODEL_METHODS:
                sid = params["sessionId"]
                if sid not in self.sessions or manager._sessions[sid].owner is not self:
                    raise LocalPolicyError("dialogue is not attached to this stream")
                if not manager.idle(sid) or any(
                    p.method in {"session/prompt"} | MODEL_METHODS
                    and p.params.get("sessionId") == sid
                    for p in manager._requests.values()
                ):
                    self.emit(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {
                                "code": MODEL_REFUSED if method in MODEL_METHODS else -32000,
                                "message": "Hermes dialogue busy or uncertain",
                            },
                        }
                    )
                    return
            elif method != "session/new":
                raise LocalPolicyError("Hermes operation unavailable")
            if len(manager._requests) >= 32 or any(
                p.owner is self and p.original_id == rid for p in manager._requests.values()
            ):
                raise LocalPolicyError("too many or duplicate managed requests")
            token = uuid4().hex
            manager._requests[token] = _Request(self, rid, method, params)
            if method == MODEL_SET:
                manager._sessions[params["sessionId"]].model_result = None
            if method in {"session/prompt"} | MODEL_METHODS:
                manager._record(params["sessionId"], "request_started", method=method)
            await manager._engine.send({**clean, "id": token})
        except LocalPolicyError:
            await self.close()
            raise

    async def receive(self):
        if self.closed.is_set():
            return None
        frame, size = await self.out.get()
        if self.closed.is_set():
            return None
        self._buffered_bytes -= size
        return frame

    async def close(self):
        self.manager.detach(self)
        self.closed.set()
        while not self.out.empty():
            self.out.get_nowait()
        self._buffered_bytes = 0
        self.out.put_nowait((None, 0))
