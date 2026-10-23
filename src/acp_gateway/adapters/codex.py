"""Managed, owned Codex App Server stdio projected onto the small ACP surface.

No attach/load, prompt replay, model discovery or unconfirmed cancellation.
Native protocol fields follow the installed Codex 0.163.0 generated schema.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from acp import PROTOCOL_VERSION

from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.config import CodexSettings
from acp_gateway.connectors.policy import LocalAgentPolicy, LocalPolicyError

MAX_FRAME = 262_144
MAX_EVENTS = 128
MAX_REQUESTS = 32
MAX_SESSIONS = 1024


@dataclass
class _Turn:
    done: asyncio.Future
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    native_id: str | None = None
    streamed: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    tools: dict[str, dict] = field(default_factory=dict)


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128 or not value.isprintable():
        raise ValueError("invalid native identity")
    return value


class CodexAcpTransport:
    def __init__(self, process, settings: CodexSettings, policy: LocalAgentPolicy):
        self.process, self.settings, self.policy = process, settings, policy
        self.closed = asyncio.Event()
        self._closing: asyncio.Task | None = None
        self._out: asyncio.Queue = asyncio.Queue(MAX_EVENTS)
        self._events: asyncio.Queue = asyncio.Queue(MAX_EVENTS)
        self._pending: dict[int, asyncio.Future] = {}
        self._requests: dict[str | int, asyncio.Task] = {}
        self._sessions: dict[str, str] = {}
        self._turns: dict[str, _Turn] = {}
        self._permissions: dict[str, tuple[str, str | int, str]] = {}
        self._counter = 0
        self._initialized = False
        self.uncertain = 0
        self.admission = None
        self._write_lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._pump = asyncio.create_task(self._pump_events())

    @classmethod
    async def open(cls, settings: CodexSettings, policy: LocalAgentPolicy):
        # Never pass gateway/relay/Telegram secrets to a local model or its tools.
        allowed = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LOCALAPPDATA",
            "APPDATA",
            "LANG",
            "LC_ALL",
            "CODEX_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        try:
            if settings.wsl is not None:
                from acp_gateway.adapters.wsl import spawn_wsl

                process = await spawn_wsl(
                    settings.wsl,
                    [settings.executable, "app-server", "--listen", "stdio://"],
                    cwd=policy.allowed_cwds[0],
                    agent_home_env="CODEX_HOME",
                    limit=MAX_FRAME,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    settings.executable,
                    "app-server",
                    "--listen",
                    "stdio://",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=policy.allowed_cwds[0],
                    env=env,
                    limit=MAX_FRAME,
                )
        except (OSError, ValueError, RuntimeError):
            raise AgentUnavailable("managed Codex executable could not be started") from None
        return cls(process, settings, policy)

    def _emit(self, frame):
        self._out.put_nowait({"jsonrpc": "2.0", **frame})

    def _update(self, sid, update):
        self._emit({"method": "session/update", "params": {"sessionId": sid, "update": update}})

    async def _write(self, frame):
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
        if len(data) > MAX_FRAME or self.closed.is_set():
            raise ConnectionError("managed Codex stream unavailable")
        async with self._write_lock:
            self.process.stdin.write(data)
            await asyncio.wait_for(self.process.stdin.drain(), self.settings.request_timeout)

    async def _rpc(self, method, params):
        if len(self._pending) >= MAX_REQUESTS:
            raise ConnectionError("managed Codex capacity exceeded")
        self._counter += 1
        rid = self._counter
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._write({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, self.settings.request_timeout)
        finally:
            self._pending.pop(rid, None)
            future.cancel()
            if future.done() and not future.cancelled():
                future.exception()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise ValueError
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError
                if "method" not in frame:
                    rid = frame.get("id")
                    if type(rid) is not int or rid not in self._pending:
                        raise ValueError
                    future = self._pending[rid]
                    if future.done():
                        raise ValueError
                    if "error" in frame:
                        future.set_exception(ConnectionError("managed Codex request failed"))
                    elif isinstance(frame.get("result"), dict):
                        future.set_result(frame["result"])
                    else:
                        raise ValueError
                else:
                    self._events.put_nowait(frame)
        except (Exception, asyncio.CancelledError):
            # Do not log native payloads or exception text (they may contain credentials).
            self._schedule_close()
        finally:
            self._schedule_close()

    async def _drain_stderr(self):
        # Discard continuously in bounded chunks. Diagnostics may contain credentials.
        while await self.process.stderr.read(4096):
            pass

    async def send(self, message: dict[str, Any]):
        if self.closed.is_set():
            raise ConnectionError("managed Codex stream closed")
        try:
            if "method" not in message:
                await self._permission_response(message)
                return
            clean = self.policy.prepare(message)
            if self.admission is not None:
                self.admission()
            rid = clean.get("id")
            if rid is None or rid in self._requests or len(self._requests) >= MAX_REQUESTS:
                raise LocalPolicyError("unsupported or duplicate Codex ACP request")
            self._requests[rid] = asyncio.create_task(self._request(clean))
        except Exception:
            await self.close()
            raise

    async def _request(self, message):
        rid, method, params = message["id"], message["method"], message["params"]
        try:
            if method == "initialize":
                if self._initialized or params["protocolVersion"] != PROTOCOL_VERSION:
                    raise ValueError
                await self._rpc(
                    "initialize",
                    {
                        "clientInfo": {"name": "acp-gateway", "version": "1"},
                        "capabilities": {"experimentalApi": False},
                    },
                )
                await self._write({"method": "initialized", "params": {}})
                self._initialized = True
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentInfo": {"name": "codex-managed", "version": "1"},
                    "agentCapabilities": {
                        "loadSession": False,
                        "sessionCapabilities": {"list": {}},
                        "_meta": {
                            "acp-gateway": {
                                "cancel": False,
                                "coldResume": False,
                                "conversationMode": "dedicated",
                            }
                        },
                    },
                }
            elif not self._initialized:
                raise ValueError
            elif method == "session/new":
                if len(self._sessions) >= MAX_SESSIONS:
                    raise ValueError
                native = {
                    "cwd": params["cwd"],
                    "approvalPolicy": "untrusted",
                    "approvalsReviewer": "user",
                    "sandbox": self.settings.sandbox,
                }
                if self.settings.model is not None:
                    native["model"] = self.settings.model
                response = await self._rpc("thread/start", native)
                sid = _identifier(response["thread"]["id"])
                if sid in self._sessions:
                    raise ValueError
                self._sessions[sid] = params["cwd"]
                result = {"sessionId": sid}
            elif method == "session/list":
                result = {
                    "sessions": [
                        {"sessionId": sid, "cwd": cwd}
                        for sid, cwd in self._sessions.items()
                        if cwd == params["cwd"]
                    ]
                }
            elif method == "session/prompt":
                result = await self._prompt(params)
            else:
                self._emit(
                    {"id": rid, "error": {"code": -32601, "message": "Codex method unavailable"}}
                )
                return
            self._emit({"id": rid, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            self._schedule_close()
        finally:
            self._requests.pop(rid, None)

    async def _prompt(self, params):
        sid = params["sessionId"]
        if sid not in self._sessions or sid in self._turns:
            raise ValueError
        turn = _Turn(asyncio.get_running_loop().create_future())
        self._turns[sid] = turn
        try:
            sandbox = {"type": "readOnly", "networkAccess": False}
            if self.settings.sandbox == "workspace-write":
                sandbox = {
                    "type": "workspaceWrite",
                    "networkAccess": False,
                    "writableRoots": [self._sessions[sid]],
                    "excludeSlashTmp": True,
                    "excludeTmpdirEnvVar": True,
                }
            response = await self._rpc(
                "turn/start",
                {
                    "threadId": sid,
                    "cwd": self._sessions[sid],
                    "input": [
                        {"type": "text", "text": "\n".join(b["text"] for b in params["prompt"])}
                    ],
                    "approvalPolicy": "untrusted",
                    "approvalsReviewer": "user",
                    "sandboxPolicy": sandbox,
                },
            )
            turn.native_id = _identifier(response["turn"]["id"])
            turn.ready.set()
            status = await asyncio.wait_for(turn.done, self.settings.turn_timeout)
            if status != "completed":
                raise ValueError
            return {"stopReason": "end_turn"}
        except BaseException:
            self.uncertain = max(1, self.uncertain)
            raise
        finally:
            turn.ready.set()
            self._turns.pop(sid, None)
            turn.done.cancel()
            if turn.done.done() and not turn.done.cancelled():
                turn.done.exception()

    def _withdraw_permissions(self, sid, upstream_id=None):
        for local_id, (owner, native_id, _) in list(self._permissions.items()):
            if owner == sid and (upstream_id is None or native_id == upstream_id):
                del self._permissions[local_id]
                self._update(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": local_id,
                        "status": "failed",
                        "_meta": {"acp-gateway": {"permissionWithdrawn": True}},
                    },
                )

    async def _permission_response(self, message):
        rid = message.get("id")
        if not isinstance(rid, str):
            raise LocalPolicyError("invalid Codex permission response")
        pending = self._permissions.pop(rid, None)
        if pending is None:
            return  # Withdrawn/settled authority never revives.
        sid, upstream_id, turn_id = pending
        turn = self._turns.get(sid)
        if turn is None or turn.native_id != turn_id or turn.done.done():
            return
        outcome = message.get("result", {}).get("outcome", {})
        decision = "decline"
        if "error" not in message and outcome.get("outcome") == "selected":
            if outcome.get("optionId") == "accept":
                decision = "accept"
            elif outcome.get("optionId") != "decline":
                raise LocalPolicyError("Codex permits once-only approval choices")
        await self._write({"id": upstream_id, "result": {"decision": decision}})

    async def _approval(self, frame, sid, turn):
        rid, params = frame["id"], frame["params"]
        if type(rid) not in (str, int) or (isinstance(rid, str) and len(rid) > 128):
            raise ValueError
        if (
            turn is None
            or turn.done.done()
            or params.get("turnId") != turn.native_id
            or params.get("grantRoot")
        ):
            await self._write({"id": rid, "result": {"decision": "decline"}})
            return
        if len(self._permissions) >= MAX_REQUESTS or any(
            p[1] == rid for p in self._permissions.values()
        ):
            raise ValueError
        raw = {k: params[k] for k in ("command", "cwd", "reason", "itemId") if k in params}
        item_id = params.get("itemId")
        item = turn.tools.get(item_id, {}) if isinstance(item_id, str) else {}
        kind = "fileChange" if "fileChange" in frame["method"] else "commandExecution"
        valid = (
            isinstance(item_id, str)
            and bool(item_id)
            and item_id not in turn.completed
            and (not item or item["type"] == kind)
        )
        if kind == "fileChange":
            changes = item.get("changes")
            valid = valid and isinstance(changes, list) and bool(changes)
            if valid:
                valid = all(
                    isinstance(change, dict)
                    and isinstance(change.get("path"), str)
                    and bool(change["path"].strip())
                    and isinstance(change.get("diff"), str)
                    and bool(change["diff"].strip())
                    and isinstance(change.get("kind"), dict)
                    and change["kind"].get("type") in {"add", "delete", "update"}
                    for change in changes
                )
            if valid:
                raw["changes"] = changes
        else:
            for key in ("command", "cwd"):
                if key not in raw and key in item:
                    raw[key] = item[key]
                valid = (
                    valid
                    and isinstance(raw.get(key), str)
                    and bool(raw[key].strip())
                    and (key not in item or raw[key] == item[key])
                )
        if not valid:
            await self._write({"id": rid, "result": {"decision": "decline"}})
            return
        local_id = "codex-permission-" + uuid4().hex
        self._permissions[local_id] = (sid, rid, turn.native_id)
        self._emit(
            {
                "id": local_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": sid,
                    "toolCall": {
                        "toolCallId": local_id,
                        "title": "Codex action",
                        "status": "pending",
                        "kind": "edit" if "fileChange" in frame["method"] else "execute",
                        "rawInput": raw,
                    },
                    "options": [
                        {"optionId": "accept", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "decline", "name": "Deny", "kind": "reject_once"},
                    ],
                },
            }
        )

    def _item(self, sid, turn, method, item):
        item_id = _identifier(item.get("id"))
        if item_id in turn.completed:
            return
        if len(turn.completed) + len(turn.streamed) + len(turn.tools) >= MAX_SESSIONS:
            raise ValueError("native item capacity exceeded")
        kind = item.get("type")
        if kind == "agentMessage" and method == "item/completed":
            text = item.get("text")
            if not isinstance(text, str):
                raise ValueError
            if item_id not in turn.streamed:
                self._update(
                    sid,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                )
        elif kind in {"commandExecution", "fileChange"}:
            if method == "item/started":
                if item_id in turn.tools:
                    return
                if len(turn.tools) >= MAX_REQUESTS:
                    raise ValueError("native tool capacity exceeded")
                raw = {k: item[k] for k in ("command", "cwd", "changes") if k in item}
                turn.tools[item_id] = {"type": kind, **raw}
                self._update(
                    sid,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": item_id,
                        "title": "Codex file changes" if kind == "fileChange" else "Codex command",
                        "kind": "edit" if kind == "fileChange" else "execute",
                        "status": "in_progress",
                        "rawInput": raw,
                    },
                )
            elif method == "item/completed":
                turn.tools.pop(item_id, None)
                self._update(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": item_id,
                        "status": "completed" if item.get("status") == "completed" else "failed",
                    },
                )
        if method == "item/completed":
            turn.completed.add(item_id)

    async def _pump_events(self):
        try:
            while True:
                frame = await self._events.get()
                params = frame.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError
                sid = params.get("threadId")
                turn = self._turns.get(sid) if isinstance(sid, str) else None
                if turn is not None:
                    await turn.ready.wait()
                method = frame["method"]
                if "id" in frame:
                    if method in {
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                    }:
                        await self._approval(frame, sid, turn)
                    else:
                        await self._write(
                            {
                                "id": frame["id"],
                                "error": {"code": -32601, "message": "Client method unavailable"},
                            }
                        )
                    continue
                if method == "serverRequest/resolved":
                    self._withdraw_permissions(sid, params.get("requestId"))
                    continue
                if turn is None:
                    continue
                native_id = (
                    params.get("turn", {}).get("id")
                    if method == "turn/completed"
                    else params.get("turnId")
                )
                if native_id != turn.native_id or turn.done.done():
                    continue
                if method == "item/agentMessage/delta":
                    item_id = _identifier(params.get("itemId"))
                    if item_id in turn.completed:
                        continue
                    if len(turn.streamed) >= MAX_SESSIONS or not isinstance(
                        params.get("delta"), str
                    ):
                        raise ValueError
                    turn.streamed.add(item_id)
                    self._update(
                        sid,
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": params["delta"]},
                        },
                    )
                elif method in {"item/started", "item/completed"}:
                    self._item(sid, turn, method, params["item"])
                elif method == "turn/completed":
                    self._withdraw_permissions(sid)
                    turn.done.set_result(params["turn"].get("status"))
        except asyncio.CancelledError:
            pass
        except Exception:
            self._schedule_close()

    async def receive(self):
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
            read.cancel()
            closed.cancel()
            await asyncio.gather(read, closed, return_exceptions=True)

    def _schedule_close(self):
        if self._closing is None:
            self.uncertain = max(self.uncertain, len(self._turns))
            self.closed.set()
            self._closing = asyncio.create_task(self._shutdown())

    async def close(self):
        self._schedule_close()
        await asyncio.shield(self._closing)

    async def _shutdown(self):
        self._permissions.clear()
        tasks = [self._reader, self._stderr, self._pump, *self._requests.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.settings.wsl is not None:
            from acp_gateway.adapters.wsl import close_wsl

            if await close_wsl(self.process) is False:
                self.uncertain = max(self.uncertain, 1)
            return
        self.process.stdin.close()
        # These are our pipe ends, not descendant processes. EOF may never arrive
        # while a tool inherits stdout/stderr, even after the owned child exits.
        process_transport = self.process._transport
        for fd in (1, 2):
            pipe = process_transport.get_pipe_transport(fd)
            if pipe is not None:
                pipe.close()
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        process_transport.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await asyncio.wait_for(self.process.stdin.wait_closed(), 2)


class CodexProcessManager:
    """Track only this connector's children; never retain resumable conversations."""

    def __init__(self, settings: CodexSettings, policy: LocalAgentPolicy):
        self.settings, self.policy = settings, policy
        self.maintenance_reserved = False
        self._streams: set[CodexAcpTransport] = set()
        self._opening = 0
        self._uncertain = 0

    def check_admission(self):
        if self.maintenance_reserved:
            raise AgentUnavailable("Connector reserved for maintenance")

    def _prune(self):
        for stream in list(self._streams):
            if stream._closing is not None and stream._closing.done():
                self._uncertain += stream.uncertain
                self._streams.remove(stream)

    async def open(self):
        self.check_admission()
        self._prune()
        if len(self._streams) + self._opening >= MAX_REQUESTS:
            raise AgentUnavailable("managed Codex process capacity exceeded")
        self._opening += 1
        try:
            stream = await CodexAcpTransport.open(self.settings, self.policy)
            stream.admission = self.check_admission
            self._streams.add(stream)
            return stream
        finally:
            self._opening -= 1

    def maintenance_status(self):
        self._prune()
        return {
            "pending_requests": sum(len(s._requests) for s in self._streams),
            "pending_approvals": sum(len(s._permissions) for s in self._streams),
            "running_sessions": sum(len(s._turns) for s in self._streams),
            "uncertain_sessions": self._uncertain + sum(s.uncertain for s in self._streams),
            "opening": self._opening,
        }

    async def close(self):
        self.maintenance_reserved = True
        await asyncio.gather(*(s.close() for s in self._streams))
        self._streams.clear()
