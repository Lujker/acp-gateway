"""Owned Claude Code stream-json conversations with scoped stored resume.

Safe uses manual permissions; work explicitly selects native automatic review.
This boundary is not a filesystem sandbox. Unknown prompts are never replayed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from acp import PROTOCOL_VERSION

from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.connectors.policy import LocalAgentPolicy, LocalPolicyError

if TYPE_CHECKING:
    from acp_gateway.config import ClaudeCodeSettings

MAX_FRAME = 262_144
# Native output lines are larger than our own requests: Claude Code reports a whole
# tool result (often over 256 KiB) in one line. A shorter limit drops the turn.
MAX_NATIVE_LINE = 16 * 1024 * 1024
MAX_EVENTS = 128
MAX_REQUESTS = 32
TOOLS = "Read,Glob,Grep,Edit,Write,Bash"
TOOL_BLOCKS = {"tool_use", "server_tool_use", "mcp_tool_use"}


class ClaudeCodeAcpTransport:
    def __init__(self, settings: ClaudeCodeSettings, policy: LocalAgentPolicy, *, sessions=None):
        self.settings, self.policy = settings, policy
        self.sessions = sessions
        self.closed = asyncio.Event()
        self._closing = None
        self._out = asyncio.Queue(MAX_EVENTS)
        self._requests = {}
        self._children = {}
        self._sessions = {}
        self._session_lock = asyncio.Lock()
        self._turns = {}
        self._permissions = {}
        self._initialized = False
        self.uncertain = 0
        self.admission = None
        self.child_reservation = None

    @classmethod
    async def open(cls, settings: ClaudeCodeSettings, policy: LocalAgentPolicy, *, sessions=None):
        stream = cls(settings, policy, sessions=sessions)
        try:
            stream._warm = await stream._spawn(policy.allowed_cwds[0])
            stream.process = stream._warm["process"]
            return stream
        except BaseException:
            await stream.close()
            raise

    async def _spawn(self, cwd, resume=None, *, session_id=None):
        if len(self._children) >= self.settings.max_sessions:
            raise AgentUnavailable("managed Claude Code process capacity exceeded")
        release = self.child_reservation() if self.child_reservation is not None else None
        try:
            return await self._spawn_owned(cwd, resume, session_id=session_id)
        finally:
            if release is not None:
                release()

    async def _spawn_owned(self, cwd, resume=None, *, session_id=None):
        sid = resume or session_id or str(uuid4())
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
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        argv = [
            self.settings.executable,
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "manual",
            "--permission-prompt-tool",
            "stdio",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--settings",
            json.dumps({"disableAllHooks": True, "permissions": {"ask": TOOLS.split(",")}}),
            f"--session-id={sid}",
            "--include-partial-messages",
            "--no-chrome",
            "--disable-slash-commands",
            "--safe-mode",
            "--tools",
            TOOLS,
            "--max-turns",
            str(self.settings.max_turns),
        ]
        if self.settings.mode == "work":
            argv = [
                self.settings.executable,
                "--print",
                "--verbose",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--permission-mode",
                "auto",
                "--permission-prompt-tool",
                "stdio",
                "--setting-sources",
                "user,project,local",
                f"--session-id={sid}",
                "--include-partial-messages",
                "--tools",
                "default",
                "--max-turns",
                str(self.settings.max_turns),
            ]
        if self.settings.model is not None:
            argv += ["--model", self.settings.model]
        if resume is not None:
            argv.remove(f"--session-id={sid}")
            argv += ["--resume", sid]
        try:
            if self.settings.wsl is not None:
                from acp_gateway.adapters.wsl import spawn_wsl

                process = await spawn_wsl(
                    self.settings.wsl,
                    argv,
                    cwd=cwd,
                    agent_home_env="CLAUDE_CONFIG_DIR",
                    limit=MAX_NATIVE_LINE,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    limit=MAX_NATIVE_LINE,
                )
        except (OSError, ValueError, RuntimeError):
            raise AgentUnavailable("managed Claude Code executable could not be started") from None
        child = {
            "process": process,
            "sid": sid,
            "cwd": cwd,
            "lock": asyncio.Lock(),
            "control_ids": set(),
            "controls": {},
        }
        self._children[sid] = child
        child["stderr"] = asyncio.create_task(self._drain_stderr(process))
        rid = uuid4().hex
        await self._write(
            child,
            {
                "type": "control_request",
                "request_id": rid,
                "request": {"subtype": "initialize", "hooks": None},
            },
        )
        try:
            frame = await asyncio.wait_for(self._frame(child), self.settings.request_timeout)
            response = frame.get("response", {})
            if (
                frame.get("type") != "control_response"
                or response.get("request_id") != rid
                or response.get("subtype") != "success"
                or not isinstance(response.get("response"), dict)
            ):
                raise ValueError
        except Exception:
            raise AgentUnavailable("managed Claude Code initialization failed") from None
        child["reader"] = asyncio.create_task(self._read(child))
        return child

    async def _frame(self, child):
        line = await child["process"].stdout.readline()
        if not line or len(line) > MAX_NATIVE_LINE or not line.endswith(b"\n"):
            raise ConnectionError("managed Claude Code stream unavailable")
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise ValueError
        return frame

    async def _write(self, child, frame, *, permission=None):
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
        if len(data) > MAX_FRAME or self.closed.is_set():
            raise ConnectionError("managed Claude Code stream unavailable")
        async with child["lock"]:
            if permission is not None:
                local_id, pending = permission
                owner, turn, _, _ = pending
                sid = child["sid"]
                if (
                    self.closed.is_set()
                    or self._permissions.get(local_id) is not pending
                    or owner is not child
                    or self._children.get(sid) is not child
                    or self._sessions.get(sid) is not child
                    or self._turns.get(sid) is not turn
                    or turn["done"].done()
                    or turn.get("cancelling", False)
                ):
                    return
                # Commit once, without yielding between validation and stdin.write.
                del self._permissions[local_id]
            child["process"].stdin.write(data)
            await asyncio.wait_for(child["process"].stdin.drain(), self.settings.request_timeout)

    async def _drain_stderr(self, process):
        while await process.stderr.read(4096):
            pass

    def _emit(self, frame):
        self._out.put_nowait({"jsonrpc": "2.0", **frame})

    async def send(self, message):
        if self.closed.is_set():
            raise ConnectionError("managed Claude Code stream closed")
        try:
            if "method" not in message:
                await self._permission_response(message)
                return
            clean = self.policy.prepare(message)
            if self.admission is not None:
                self.admission()
            if clean["method"] == "session/cancel":
                clean["id"] = "cancel-" + uuid4().hex
            rid = clean.get("id")
            if rid is None or rid in self._requests or len(self._requests) >= MAX_REQUESTS:
                raise LocalPolicyError("unsupported or duplicate Claude Code ACP request")
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
                self._initialized = True
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentInfo": {"name": "claude-code-managed", "version": "1"},
                    "agentCapabilities": {
                        "loadSession": self.sessions is not None,
                        "sessionCapabilities": {"list": {}},
                        "_meta": {
                            "acp-gateway": {
                                "cancel": True,
                                "coldResume": self.sessions is not None,
                                "conversationMode": "dedicated",
                                "profileMode": self.settings.mode,
                                "executionEnvironment": "wsl" if self.settings.wsl else "native",
                            }
                        },
                    },
                }
            elif not self._initialized:
                raise ValueError
            elif method == "session/new":
                async with self._session_lock:
                    if len(self._sessions) >= self.settings.max_sessions:
                        raise ValueError
                    if self._warm is not None and self._warm["cwd"] == params["cwd"]:
                        child, self._warm = self._warm, None
                    else:
                        child = await self._spawn(params["cwd"])
                    if self.sessions is not None:
                        self.sessions.create(child["sid"], params["cwd"], empty=True)
                    self._sessions[child["sid"]] = child
                result = {"sessionId": child["sid"]}
            elif method == "session/load":
                sid = params["sessionId"]
                # Claude's --resume accepts a native UUID, never a path or option.
                if str(UUID(sid)) != sid:
                    raise ValueError
                async with self._session_lock:
                    if sid in self._sessions:
                        if self._sessions[sid]["cwd"] != params["cwd"] or sid in self._turns:
                            raise ValueError
                    else:
                        try:
                            if self.sessions is None:
                                raise LocalPolicyError("resume is unavailable")
                            has_history = self.sessions.load(sid, params["cwd"])
                        except LocalPolicyError:
                            self._emit(
                                {
                                    "id": rid,
                                    "error": {
                                        "code": -32001,
                                        "message": "Native binding unavailable or outcome unknown",
                                    },
                                }
                            )
                            return
                        if self._warm is not None:
                            warm, self._warm = self._warm, None
                            for key in ("reader", "stderr"):
                                warm[key].cancel()
                            await asyncio.gather(
                                warm["reader"], warm["stderr"], return_exceptions=True
                            )
                            await self._stop_process(warm["process"])
                            if self.uncertain:
                                raise AgentUnavailable("WSL process cleanup is unconfirmed")
                            self._children.pop(warm["sid"])
                        # Claude has no persisted transcript until the first prompt.
                        # Only explicit empty bindings may reuse the ID as a new CLI
                        # session. Legacy/used bindings always require native resume.
                        self._sessions[sid] = (
                            await self._spawn(params["cwd"], sid)
                            if has_history
                            else await self._spawn(params["cwd"], session_id=sid)
                        )
                result = {}
            elif method == "session/list":
                result = {
                    "sessions": [
                        {"sessionId": sid, "cwd": child["cwd"]}
                        for sid, child in self._sessions.items()
                        if child["cwd"] == params["cwd"]
                    ]
                }
            elif method == "session/prompt":
                result = await self._prompt(params)
            elif method == "session/cancel":
                await self._cancel(params["sessionId"])
                return
            else:
                self._emit(
                    {
                        "id": rid,
                        "error": {"code": -32601, "message": "Claude Code method unavailable"},
                    }
                )
                return
            self._emit({"id": rid, "result": result})
        except AgentUnavailable:
            if method == "session/load":
                with contextlib.suppress(asyncio.QueueFull):
                    self._emit(
                        {
                            "id": rid,
                            "error": {
                                "code": -32001,
                                "message": "Native session history unavailable or launch failed; "
                                "create a new dialogue explicitly",
                            },
                        }
                    )
            self._schedule_close()
        except Exception:
            self._schedule_close()
        finally:
            self._requests.pop(rid, None)

    def _text(self, sid, text, *, work=False):
        if not isinstance(text, str):
            raise ValueError
        self._emit(
            {
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk" if work else "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                },
            }
        )

    @staticmethod
    def _new_turn(done):
        return {
            "done": done,
            "answered": False,
            "worked": False,
            "messages": set(),
            "streamed": set(),
            "message_id": None,
            "pending": [],
            "pending_chars": 0,
            "cancelling": False,
            "interrupt_acked": False,
            "cancel_result": None,
        }

    # Claude Code sends one `assistant` frame per content block (thinking, text,
    # tool_use), all with the same API message id. Text is held per message until
    # it is known whether a tool call follows: then it is a work update, otherwise
    # it is answer text. Thinking is never forwarded.
    def _message(self, sid, turn, mid):
        if not isinstance(mid, str) or not mid or len(mid) > 128:
            raise ValueError
        if mid == turn["message_id"]:
            return
        self._flush(sid, turn, work=False)
        if mid not in turn["messages"]:
            if len(turn["messages"]) >= MAX_EVENTS:
                raise ValueError
            turn["messages"].add(mid)
        turn["message_id"] = mid

    def _hold(self, turn, text):
        if not isinstance(text, str):
            raise ValueError
        turn["pending_chars"] += len(text)
        if turn["pending_chars"] > MAX_FRAME:
            raise ValueError("managed Claude Code message buffer exceeded")
        turn["pending"].append(text)

    def _flush(self, sid, turn, *, work):
        text = "".join(turn["pending"])
        turn["pending"].clear()
        turn["pending_chars"] = 0
        if not text.strip():
            return
        key = "worked" if work else "answered"
        prefix = "\n\n" if turn[key] else ""
        turn[key] = True
        self._text(sid, prefix + text, work=work)

    async def _prompt(self, params):
        sid = params["sessionId"]
        if sid not in self._sessions or sid in self._turns:
            raise ValueError
        child = self._sessions[sid]
        turn = self._new_turn(asyncio.get_running_loop().create_future())
        self._turns[sid] = turn
        try:
            if self.sessions is not None:
                self.sessions.begin(sid)
            await self._write(
                child,
                {
                    "type": "user",
                    "session_id": sid,
                    "parent_tool_use_id": None,
                    "message": {
                        "role": "user",
                        "content": "\n".join(b["text"] for b in params["prompt"]),
                    },
                },
            )
            reason = await asyncio.wait_for(turn["done"], self.settings.turn_timeout)
            if self.sessions is not None:
                self.sessions.finish(sid)
            return {"stopReason": reason}
        except BaseException:
            self.uncertain = max(1, self.uncertain)
            raise
        finally:
            self._turns.pop(sid, None)
            turn["done"].cancel()
            if turn["done"].done() and not turn["done"].cancelled():
                turn["done"].exception()

    async def _cancel(self, sid):
        turn, child = self._turns.get(sid), self._sessions.get(sid)
        if turn is None or child is None or turn["done"].done() or turn["cancelling"]:
            return
        turn["cancelling"] = True
        self._withdraw(child)
        rid = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        child["controls"][rid] = future
        try:
            await self._write(
                child,
                {"type": "control_request", "request_id": rid, "request": {"subtype": "interrupt"}},
            )
            await asyncio.wait_for(future, self.settings.request_timeout)
            turn["interrupt_acked"] = True
            if turn["cancel_result"] is not None:
                self._finish_result(child, turn, turn["cancel_result"])
        finally:
            child["controls"].pop(rid, None)
            future.cancel()

    def _finish_result(self, child, turn, frame):
        self._withdraw(child)
        if turn.get("cancelling", False) and not turn.get("interrupt_acked", False):
            turn["cancel_result"] = frame
            return
        subtype, is_error = frame.get("subtype"), frame.get("is_error")
        normal = subtype == "success" and is_error is False
        # The native turn limit ends the turn, not the conversation.
        limited = subtype == "error_max_turns" and isinstance(is_error, bool)
        interrupted = (
            turn.get("interrupt_acked", False)
            and subtype == "error_during_execution"
            and is_error is True
        )
        if not normal and not limited and not interrupted:
            raise ValueError
        sid = child["sid"]
        self._flush(sid, turn, work=False)
        if normal and not turn["answered"] and isinstance(frame.get("result"), str):
            self._hold(turn, frame["result"])
            self._flush(sid, turn, work=False)
        if turn.get("cancelling", False):
            reason = "cancelled"
        else:
            reason = "max_turn_requests" if limited else "end_turn"
        turn["done"].set_result(reason)

    def _withdraw(self, child, native_id=None):
        for local_id, (owner, _, rid, _) in list(self._permissions.items()):
            if owner is child and (native_id is None or native_id == rid):
                del self._permissions[local_id]
                self._emit(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": child["sid"],
                            "update": {
                                "sessionUpdate": "tool_call_update",
                                "toolCallId": local_id,
                                "status": "failed",
                                "_meta": {"acp-gateway": {"permissionWithdrawn": True}},
                            },
                        },
                    }
                )

    async def _control_reply(self, child, rid, response, *, permission=None):
        await self._write(
            child,
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": rid, "response": response},
            },
            permission=permission,
        )

    async def _approval(self, child, turn, frame):
        rid, request = frame["request_id"], frame["request"]
        if not isinstance(rid, str) or not rid or len(rid) > 128:
            raise ValueError
        if rid in child["control_ids"] or len(child["control_ids"]) >= MAX_EVENTS:
            raise ValueError
        child["control_ids"].add(rid)
        if request.get("subtype") != "can_use_tool":
            await self._write(
                child,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "error",
                        "request_id": rid,
                        "error": "Unsupported control request",
                    },
                },
            )
            return
        local_id = "claude-permission-" + uuid4().hex
        tool, data = request.get("tool_name"), request.get("input")
        fields = {
            "Read": ("file_path",),
            "Glob": ("pattern",),
            "Grep": ("pattern",),
            "Bash": ("command",),
            "Write": ("file_path", "content"),
            "Edit": ("file_path", "old_string", "new_string"),
        }
        valid = (
            isinstance(tool, str)
            and tool in fields
            and isinstance(data, dict)
            and (tool != "Bash" or data.get("run_in_background", False) is False)
            and all(isinstance(data.get(key), str) for key in fields.get(tool, ()))
            and all(
                data.get(key, "").strip()
                for key in fields.get(tool, ())
                if key not in {"content", "old_string", "new_string"}
            )
            and isinstance(request.get("tool_use_id"), str)
            and bool(request["tool_use_id"])
            and request.get("agent_id") is None
            and request.get("parent_tool_use_id") is None
            and request.get("session_id", child["sid"]) == child["sid"]
            and frame.get("session_id", child["sid"]) == child["sid"]
        )
        if self.settings.mode == "work":
            valid = (
                isinstance(tool, str)
                and bool(tool)
                and len(tool) <= 128
                and tool.isprintable()
                and isinstance(data, dict)
                and isinstance(request.get("tool_use_id"), str)
                and bool(request["tool_use_id"])
                and request.get("session_id", child["sid"]) == child["sid"]
                and frame.get("session_id", child["sid"]) == child["sid"]
            )
        if turn is None or turn["done"].done() or turn.get("cancelling", False) or not valid:
            await self._control_reply(
                child, rid, {"behavior": "deny", "message": "Denied by gateway"}
            )
            return
        if len(self._permissions) >= MAX_REQUESTS:
            raise ValueError
        self._permissions[local_id] = (child, turn, rid, request["input"])
        self._emit(
            {
                "id": local_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": child["sid"],
                    "toolCall": {
                        "toolCallId": local_id,
                        "title": "Claude Code " + request["tool_name"],
                        "status": "pending",
                        "kind": {
                            "Bash": "execute",
                            "Edit": "edit",
                            "Write": "edit",
                            "Read": "read",
                            "Glob": "search",
                            "Grep": "search",
                        }.get(tool, "other"),
                        "rawInput": {
                            "tool_name": request["tool_name"],
                            "tool_use_id": request["tool_use_id"],
                            "input": request["input"],
                            "cwd": child["cwd"],
                        },
                    },
                    "options": [
                        {"optionId": "accept", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "decline", "name": "Deny", "kind": "reject_once"},
                    ],
                },
            }
        )

    async def _permission_response(self, message):
        local_id = message.get("id")
        pending = self._permissions.get(local_id)
        if pending is None:
            return
        child, turn, rid, original = pending
        if self._turns.get(child["sid"]) is not turn or turn["done"].done():
            return
        outcome = message.get("result", {}).get("outcome", {})
        response = {"behavior": "deny", "message": "Denied by gateway"}
        if "error" not in message and outcome.get("outcome") == "selected":
            if outcome.get("optionId") == "accept":
                response = {"behavior": "allow", "updatedInput": original}
            elif outcome.get("optionId") != "decline":
                raise LocalPolicyError("Claude Code permits once-only approval choices")
        await self._control_reply(child, rid, response, permission=(local_id, pending))

    async def _read(self, child):
        sid = child["sid"]
        try:
            while True:
                frame = await self._frame(child)
                turn = self._turns.get(sid)
                if frame.get("type") == "control_response":
                    response = frame.get("response", {})
                    future = child["controls"].get(response.get("request_id"))
                    if future is not None and not future.done():
                        if response.get("subtype") != "success":
                            raise ValueError
                        future.set_result(None)
                    continue
                if frame.get("type") == "control_cancel_request":
                    self._withdraw(child, frame["request_id"])
                    continue
                if frame.get("type") == "control_request":
                    await self._approval(child, turn, frame)
                    continue
                if (
                    turn is None
                    or turn["done"].done()
                    or frame.get("session_id") != sid
                    or frame.get("parent_tool_use_id") is not None
                ):
                    continue
                if frame.get("type") == "stream_event":
                    event = frame["event"]
                    if event.get("type") == "message_start":
                        self._message(sid, turn, event["message"]["id"])
                    elif event.get("type") == "content_block_start":
                        if event.get("content_block", {}).get("type") in TOOL_BLOCKS:
                            self._flush(sid, turn, work=True)
                    elif (
                        event.get("type") == "content_block_delta"
                        and event.get("delta", {}).get("type") == "text_delta"
                    ):
                        if turn["message_id"] is None:
                            raise ValueError
                        turn["streamed"].add(turn["message_id"])
                        self._hold(turn, event["delta"]["text"])
                elif frame.get("type") == "assistant":
                    if frame.get("error"):
                        raise ValueError("managed Claude Code assistant failed")
                    mid = frame["message"]["id"]
                    self._message(sid, turn, mid)
                    for block in frame["message"]["content"]:
                        if block.get("type") == "text" and mid not in turn["streamed"]:
                            self._hold(turn, block["text"])
                        elif block.get("type") in TOOL_BLOCKS:
                            self._flush(sid, turn, work=True)
                elif frame.get("type") == "result":
                    self._finish_result(child, turn, frame)
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
            self.uncertain = max(
                self.uncertain, sum(not t["done"].done() for t in self._turns.values())
            )
            self.closed.set()
            self._closing = asyncio.create_task(self._shutdown())

    async def close(self):
        self._schedule_close()
        await asyncio.shield(self._closing)

    async def _shutdown(self):
        confirmed = False
        try:
            await self._shutdown_children()
            confirmed = self.uncertain == 0
        finally:
            if self.sessions is not None:
                self.sessions.close(confirmed=confirmed)

    async def _shutdown_children(self):
        self._permissions.clear()
        tasks = [
            *self._requests.values(),
            *(
                task
                for c in self._children.values()
                for key in ("stderr", "reader")
                if (task := c.get(key)) is not None
            ),
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        results = await asyncio.gather(
            *(self._stop_process(child["process"]) for child in self._children.values()),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            raise AgentUnavailable("managed Claude Code process cleanup is unconfirmed")

    async def _stop_process(self, process):
        if self.settings.wsl is not None:
            from acp_gateway.adapters.wsl import close_wsl

            if await close_wsl(process) is False:
                self.uncertain = max(self.uncertain, 1)
            return
        process.stdin.close()
        transport = process._transport
        for fd in (1, 2):
            pipe = transport.get_pipe_transport(fd)
            if pipe is not None:
                pipe.close()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        transport.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await asyncio.wait_for(process.stdin.wait_closed(), 2)


class ClaudeCodeProcessManager:
    """Account only for owned processes; retain uncertainty after binding loss."""

    def __init__(self, settings: ClaudeCodeSettings, policy: LocalAgentPolicy, *, sessions=None):
        self.settings, self.policy = settings, policy
        self.sessions = sessions
        self.maintenance_reserved = False
        self._streams = set()
        self._opening = 0
        self._spawning = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._uncertain = 0

    def check_admission(self):
        if self.maintenance_reserved:
            raise AgentUnavailable("Connector reserved for maintenance")

    def _prune(self):
        for stream in list(self._streams):
            if stream._closing is not None and stream._closing.done():
                self._uncertain += stream.uncertain
                self._streams.remove(stream)

    def _capacity(self):
        self.check_admission()
        self._prune()
        if (
            self._opening + self._spawning + sum(len(s._children) for s in self._streams)
            >= self.settings.max_sessions
        ):
            raise AgentUnavailable("managed Claude Code process capacity exceeded")

    def _reserve_child(self):
        self._capacity()
        self._spawning += 1
        self._idle.clear()

        def release():
            self._spawning -= 1
            if not self._opening and not self._spawning:
                self._idle.set()

        return release

    async def open(self):
        self._capacity()
        self._opening += 1
        self._idle.clear()
        try:
            stream = await ClaudeCodeAcpTransport.open(
                self.settings,
                self.policy,
                sessions=self.sessions.fork() if self.sessions is not None else None,
            )
            if self.maintenance_reserved:
                await stream.close()
                self.check_admission()
            stream.admission = self.check_admission
            stream.child_reservation = self._reserve_child
            self._streams.add(stream)
            return stream
        finally:
            self._opening -= 1
            if not self._opening and not self._spawning:
                self._idle.set()

    def maintenance_status(self):
        self._prune()
        return {
            "pending_requests": sum(len(s._requests) for s in self._streams),
            "pending_approvals": sum(len(s._permissions) for s in self._streams),
            "running_sessions": sum(len(s._turns) for s in self._streams),
            "uncertain_sessions": self._uncertain + sum(s.uncertain for s in self._streams),
            "opening": self._opening + self._spawning,
        }

    async def close(self):
        self.maintenance_reserved = True
        await self._idle.wait()
        await asyncio.gather(*(s.close() for s in self._streams))
        self._prune()
