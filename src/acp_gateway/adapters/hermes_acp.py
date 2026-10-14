"""ACP stream over dispatcher-owned Hermes conversations.

This bridge exposes text prompts and once-only approvals, never the upstream
administrative API. No session/load, session cancellation or prompt replay.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from acp import PROTOCOL_VERSION

from acp_gateway.adapters.hermes import HermesDedicatedClient
from acp_gateway.adapters.hermes_models import HermesModelRefused
from acp_gateway.agents.bindings import session_binding
from acp_gateway.agents.model_control import MODEL_METHODS, MODEL_OPTIONS, MODEL_REFUSED, MODEL_SET
from acp_gateway.connectors.policy import LocalAgentPolicy, LocalPolicyError
from acp_gateway.discovery.hermes import HermesDiscoveryError
from acp_gateway.discovery.hermes_rpc import HermesRpcError


@dataclass
class _Turn:
    done: asyncio.Future
    streamed: bool = False


class HermesAcpTransport:
    def __init__(self, driver: HermesDedicatedClient, policy: LocalAgentPolicy):
        self.driver = driver
        self.policy = policy
        self.closed = asyncio.Event()
        self.failure: str | None = None
        self._initialized = False
        self._out: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)
        self._requests: dict[str | int, asyncio.Task] = {}
        self._sessions: dict[str, str] = {}
        self._turns: dict[str, _Turn] = {}
        self._permissions: dict[str, tuple[str, str | int, tuple[str, ...]]] = {}
        self._overflow_close: asyncio.Task | None = None
        self._pump = asyncio.create_task(self._events())

    def forget_session(self, handle: str) -> None:
        if handle not in self._sessions or handle in self._turns:
            raise HermesDiscoveryError("Hermes attachment is unavailable or running")
        self.driver.forget(handle)
        del self._sessions[handle]

    def _emit(self, frame: dict) -> None:
        try:
            self._out.put_nowait({"jsonrpc": "2.0", **frame})
        except asyncio.QueueFull:
            if self.failure is None:
                self.failure = "outbound_capacity"
                self._overflow_close = asyncio.create_task(self.close())

    def _update(self, session: str, update: dict) -> None:
        self._emit({"method": "session/update", "params": {"sessionId": session, "update": update}})

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed.is_set():
            raise ConnectionError("Hermes ACP stream is closed")
        try:
            if isinstance(message, dict) and "method" not in message:
                await self._permission_response(message)
                return
            clean = self.policy.prepare(message)
            if clean["method"] == "session/cancel":
                raise LocalPolicyError("Hermes cannot safely cancel an ordinary conversation turn")
            rid = clean["id"]
            if rid in self._requests or len(self._requests) >= 32:
                raise LocalPolicyError("too many or duplicate Hermes ACP requests")
            task = asyncio.create_task(self._request(clean))
            self._requests[rid] = task
        except (LocalPolicyError, asyncio.QueueFull):
            await self.close()
            raise

    async def _request(self, message: dict) -> None:
        rid, method, params = message["id"], message["method"], message["params"]
        try:
            if method == "initialize":
                if self._initialized or params["protocolVersion"] != PROTOCOL_VERSION:
                    raise HermesDiscoveryError("unsupported or repeated ACP initialization")
                self._initialized = True
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentInfo": {"name": "hermes-attach", "version": self.driver._rpc.version},
                    "agentCapabilities": {
                        "loadSession": False,
                        "sessionCapabilities": {"list": {}},
                        "_meta": {
                            "acp-gateway": {
                                "cancel": False,
                                "coldResume": False,
                                "conversationMode": "dedicated",
                                "approvalSettlementConfirmed": False,
                                "approvalSource": "upstream",
                                "modelControl": 1,
                            }
                        },
                    },
                }
            elif not self._initialized:
                raise HermesDiscoveryError("initialize ACP before using Hermes")
            elif method == "session/new":
                chat = await self.driver.create(cwd=params["cwd"])
                self._sessions[chat.handle] = chat.cwd
                binding = session_binding(
                    {
                        "backend": "hermes",
                        "mode": "dedicated",
                        "profile": chat.profile,
                        "runtime_id": chat.runtime_id,
                        "stored_id": chat.stored_id,
                        "restore": "disabled",
                    }
                )
                result = {
                    "sessionId": chat.handle,
                    "_meta": {"acp-gateway": {"sessionBinding": binding}},
                }
            elif method == "session/list":
                result = {
                    "sessions": [
                        {"sessionId": sid, "cwd": cwd, "title": self.driver.title(sid)}
                        for sid, cwd in self._sessions.items()
                        if cwd == params["cwd"]
                    ]
                }
            elif method == "session/prompt":
                result = await self._prompt(params)
            elif method in MODEL_METHODS:
                session = params["sessionId"]
                if session not in self._sessions:
                    raise HermesDiscoveryError("Hermes conversation is not attached")
                if self.driver.state(session) != "idle" or (
                    method == MODEL_SET and self.driver.model_activity(session)
                ):
                    self._emit(
                        {
                            "id": rid,
                            "error": {
                                "code": MODEL_REFUSED,
                                "message": "Hermes model operation refused before execution",
                            },
                        }
                    )
                    return
                if method == MODEL_OPTIONS:
                    result = await self.driver.model_options(session)
                else:
                    result = await self.driver.set_model(
                        session,
                        provider=params["provider"],
                        model=params["model"],
                        confirmation_token=params.get("confirmationToken"),
                    )
            else:
                self._emit(
                    {
                        "id": rid,
                        "error": {"code": -32601, "message": "ACP method unavailable for Hermes"},
                    }
                )
                return
            self._emit({"id": rid, "result": result})
        except HermesModelRefused:
            self._emit(
                {
                    "id": rid,
                    "error": {
                        "code": MODEL_REFUSED,
                        "message": "Hermes model operation refused before execution",
                    },
                }
            )
        except HermesRpcError as exc:
            self._emit(
                {
                    "id": rid,
                    "error": {
                        "code": -32601 if exc.code == -32601 else -32000,
                        "message": "Hermes model control unavailable or outcome unknown",
                    },
                }
            )
        except (HermesDiscoveryError, LocalPolicyError):
            self._emit(
                {
                    "id": rid,
                    "error": {
                        "code": -32000,
                        "message": "Hermes operation failed; no automatic retry",
                    },
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.close()
        finally:
            self._requests.pop(rid, None)

    async def _prompt(self, params: dict) -> dict:
        session = params["sessionId"]
        if session not in self._sessions:
            raise HermesDiscoveryError("Hermes conversation is not attached")
        if session in self._turns:
            raise HermesDiscoveryError("Hermes conversation already has a turn")
        turn = _Turn(asyncio.get_running_loop().create_future())
        self._turns[session] = turn
        try:
            previous_title = self.driver.title(session)
            await self.driver.submit(session, "\n".join(b["text"] for b in params["prompt"]))
            if previous_title is None and (title := self.driver.title(session)):
                self._update(session, {"sessionUpdate": "session_info_update", "title": title})
            payload = await turn.done
            if payload.get("status") != "complete":
                raise HermesDiscoveryError("Hermes turn failed")
            if not turn.streamed and isinstance(payload.get("text"), str) and payload["text"]:
                self._update(
                    session,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": payload["text"]},
                    },
                )
            return {"stopReason": "end_turn"}
        finally:
            turn.done.cancel()
            if turn.done.done() and not turn.done.cancelled():
                turn.done.exception()
            self._turns.pop(session, None)
            self._clear_permissions(session)

    def _clear_permissions(self, session: str) -> None:
        for rid, (owner, _, _) in list(self._permissions.items()):
            if owner == session:
                del self._permissions[rid]
                self._withdraw(session, rid)

    def _withdraw(self, session: str, rid: str) -> None:
        self._update(
            session,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": rid,
                "status": "failed",
                "_meta": {"acp-gateway": {"permissionWithdrawn": True}},
            },
        )

    async def _permission_response(self, message: dict) -> None:
        if message.get("jsonrpc") != "2.0" or "error" in message:
            raise LocalPolicyError("invalid Hermes ACP permission response")
        rid = message.get("id")
        if not isinstance(rid, str):
            raise LocalPolicyError("invalid Hermes ACP permission response")
        pending = self._permissions.get(rid)
        if pending is None:
            return  # Expired/completed requests never regain authority.
        session, upstream_id, choices = pending
        result = message.get("result")
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if not isinstance(outcome, dict) or outcome.get("outcome") not in {"selected", "cancelled"}:
            raise LocalPolicyError("invalid Hermes ACP permission outcome")
        choice = "deny"
        if outcome.get("outcome") == "selected":
            choice = outcome.get("optionId")
        if choice not in choices:
            raise LocalPolicyError("only once-only Hermes approval decisions are allowed")
        del self._permissions[rid]
        await self.driver.respond_approval(session, upstream_id, choice)

    async def _events(self) -> None:
        read = asyncio.create_task(self.driver.events.get())
        closed = asyncio.create_task(self.driver.closed.wait())
        try:
            while True:
                done, _ = await asyncio.wait({read, closed}, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    break
                event = read.result()
                read = asyncio.create_task(self.driver.events.get())
                session, kind = event["handle"], event["type"]
                payload = event.get("payload") or {}
                turn = self._turns.get(session)
                if turn is None:
                    continue
                if kind in {"message.delta", "reasoning.delta", "thinking.delta"}:
                    text = payload.get("text")
                    if isinstance(text, str) and text:
                        if kind == "message.delta":
                            turn.streamed = True
                        self._update(
                            session,
                            {
                                "sessionUpdate": "agent_message_chunk"
                                if kind == "message.delta"
                                else "agent_thought_chunk",
                                "content": {"type": "text", "text": text},
                            },
                        )
                elif kind == "message.complete" and not turn.done.done():
                    turn.done.set_result(payload)
                elif kind == "gateway.uncertain" and not turn.done.done():
                    turn.done.set_exception(
                        HermesDiscoveryError("Hermes turn ownership is uncertain")
                    )
                elif kind == "approval.request":
                    if len(self._permissions) >= 32:
                        raise ValueError
                    rid = f"hermes-permission-{uuid4().hex}"
                    choices = tuple(payload["choices"])
                    self._permissions[rid] = (session, payload["id"], choices)
                    self._emit(
                        {
                            "id": rid,
                            "method": "session/request_permission",
                            "params": {
                                "sessionId": session,
                                "toolCall": {
                                    "toolCallId": rid,
                                    "title": str(payload.get("description") or "Hermes action"),
                                    "kind": "execute",
                                    "status": "pending",
                                    "rawInput": {"command": payload.get("command", "")},
                                },
                                "options": [
                                    {
                                        "optionId": c,
                                        "name": "Allow once" if c == "once" else "Deny",
                                        "kind": "allow_once" if c == "once" else "reject_once",
                                    }
                                    for c in choices
                                ],
                            },
                        }
                    )
                elif kind == "request.cancel":
                    for rid, (owner, upstream_id, _) in list(self._permissions.items()):
                        if owner == session and upstream_id == payload.get("id"):
                            del self._permissions[rid]
                            self._withdraw(session, rid)
                elif kind == "tool.start":
                    self._update(
                        session,
                        {
                            "sessionUpdate": "tool_call",
                            "toolCallId": str(payload["tool_id"]),
                            "title": str(payload["name"]),
                            "status": "in_progress",
                            "rawInput": payload.get("args"),
                        },
                    )
                elif kind == "tool.complete":
                    self._update(
                        session,
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": str(payload["tool_id"]),
                            "status": "completed",
                        },
                    )
        except Exception:
            self.failure = "Hermes_event_translation_failed"
        finally:
            for task in (read, closed):
                task.cancel()
            await asyncio.gather(read, closed, return_exceptions=True)
            await self.close()

    async def receive(self) -> dict | None:
        if not self._out.empty():
            return self._out.get_nowait()
        if self.closed.is_set():
            return None
        read = asyncio.create_task(self._out.get())
        closed = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait({read, closed}, return_when=asyncio.FIRST_COMPLETED)
            return read.result() if read in done else None
        finally:
            for task in (read, closed):
                task.cancel()
            await asyncio.gather(read, closed, return_exceptions=True)

    async def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self._permissions.clear()
        current = asyncio.current_task()
        tasks = [t for t in (*self._requests.values(), self._pump) if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.driver.close()
