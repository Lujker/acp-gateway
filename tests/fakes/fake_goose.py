"""Mock goose agent (P1.2) served over WebSocket/HTTP like `goose serve`.

Message shapes come from recorded goose 1.53.0 traffic
(``tests/fixtures/acp/goose-1.53.0``): the ``initialize`` result, session
modes and config options, the ``tool_call`` / ``request_permission`` /
``tool_call_update`` payloads, the extra updates around a turn
(``session_info_update``, ``usage_update``, ``available_commands_update``,
``current_mode_update``) and prompt usage. Behaviour follows what the spike
observed: sessions start in ``auto``; ``approve`` and ``smart_approve`` ask
before shell commands; cancel works at any stage; ``session/load`` replays
history before answering; a session must be loaded on a new connection
before it can be prompted.

Prompts are keyword-driven so tests can trigger each path:
``run exactly: <cmd> .`` runs a shell command (``sleep N`` waits, ``echo X``
prints X), ``code word X`` / ``What was the code word`` exercise memory,
``pong`` replies "pong", anything else is echoed.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import re
import socket
import threading
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from acp import RequestError
from acp.http.asgi import create_asgi_app
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    SetSessionModeResponse,
    TextContentBlock,
    ToolCallUpdate,
    UserMessageChunk,
)

from fakes import recordings as rec

APPROVAL_MODES = {"approve", "smart_approve"}
_session_counter = itertools.count(1)


def _session_id() -> str:
    return f"20261006_{next(_session_counter)}"


@dataclass
class FakeSession:
    cwd: str
    mode: str
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    memory: dict[str, str] = field(default_factory=dict)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class FakeGooseAgent:
    """One instance per ACP connection; sessions are shared by the server."""

    def __init__(self, conn: Any, sessions: dict[str, FakeSession], log: list[str]) -> None:
        self._conn = conn
        self._sessions = sessions
        self._log = log
        self._attached: set[str] = set()
        self.client_capabilities: Any = None
        self.mcp_servers_seen: list[int] = []

    # -------------------------------------------------------------- lifecycle

    async def initialize(self, protocol_version: int, client_capabilities=None, **kw: Any):
        self.client_capabilities = client_capabilities
        self._log.append("initialize")
        result = rec.result_of("init", "initialize")
        result["protocolVersion"] = protocol_version
        return InitializeResponse.model_validate(result)

    async def new_session(self, cwd: str, mcp_servers=None, **kw: Any):
        self.mcp_servers_seen.append(len(mcp_servers or []))
        self._log.append(f"new_session mcp_servers={len(mcp_servers or [])}")
        result = rec.result_of("modes", "session/new")
        session_id = _session_id()
        result["sessionId"] = session_id
        self._sessions[session_id] = FakeSession(cwd=cwd, mode=result["modes"]["currentModeId"])
        self._attached.add(session_id)
        return NewSessionResponse.model_validate(result)

    async def load_session(self, cwd: str, session_id: str, mcp_servers=None, **kw: Any):
        self.mcp_servers_seen.append(len(mcp_servers or []))
        session = self._session(session_id, attached=False)
        self._log.append("load_session")
        for role, text in session.history:
            if role == "user":
                update: Any = UserMessageChunk(
                    session_update="user_message_chunk",
                    content=TextContentBlock(type="text", text=text),
                )
            else:
                update = AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=text),
                )
            await self._conn.session_update(session_id=session_id, update=update)
        self._attached.add(session_id)
        result = rec.result_of("load-ws", "session/load")
        result["modes"]["currentModeId"] = session.mode
        return LoadSessionResponse.model_validate(result)

    async def list_sessions(self, cwd=None, cursor=None, **kw: Any):
        from acp.schema import ListSessionsResponse

        sessions = [
            {"sessionId": sid, "cwd": s.cwd, "title": "fake session"}
            for sid, s in self._sessions.items()
            if cwd is None or s.cwd == cwd
        ]
        return ListSessionsResponse.model_validate({"sessions": sessions})

    async def set_session_mode(self, session_id: str, mode_id: str, **kw: Any):
        session = self._session(session_id)
        available = [
            m["id"] for m in rec.result_of("modes", "session/new")["modes"]["availableModes"]
        ]
        if mode_id not in available:
            raise RequestError.invalid_params({"mode": mode_id})
        session.mode = mode_id
        self._log.append(f"set_mode {mode_id}")
        await self._emit(session_id, rec.update("permission-reject", "available_commands_update"))
        mode_update = rec.update("permission-reject", "current_mode_update")
        mode_update["currentModeId"] = mode_id
        await self._emit(session_id, mode_update)
        return SetSessionModeResponse()

    async def cancel(self, session_id: str, **kw: Any) -> None:
        self._log.append("cancel")
        if session_id in self._sessions:
            self._sessions[session_id].cancelled.set()

    # ------------------------------------------------------------------ turns

    async def prompt(self, session_id: str, prompt: list[Any], **kw: Any):
        session = self._session(session_id)
        session.cancelled.clear()
        text = " ".join(getattr(block, "text", "") for block in prompt)
        session.history.append(("user", text))
        await self._emit(session_id, rec.update("ping", "session_info_update", 0))

        if match := re.search(r"run exactly(?: this command)?: (.+?) \.", text):
            return await self._shell(session_id, session, match.group(1))
        if match := re.search(r"code word (\w+)", text):
            session.memory["word"] = match.group(1)
            return await self._reply(session_id, "OK")
        if "What was the code word" in text:
            return await self._reply(session_id, session.memory.get("word", "unknown"))
        if "pong" in text:
            return await self._reply(session_id, "pong")
        return await self._reply(session_id, f"echo: {text}")

    async def _shell(self, session_id: str, session: FakeSession, command: str) -> PromptResponse:
        tool_call_id = f"call_{next(_session_counter):08d}"
        title = f"shell · {command}"
        call = rec.update("permission-reject", "tool_call")
        call.update(toolCallId=tool_call_id, title=title)
        call["rawInput"]["command"] = command
        await self._emit(session_id, call)

        if session.mode in APPROVAL_MODES:
            params = rec.request("permission-reject", "session/request_permission")
            tool_call = params["toolCall"]
            tool_call.update(toolCallId=tool_call_id, title=title)
            tool_call["rawInput"]["command"] = command
            response = await self._conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate.model_validate(tool_call),
                options=[PermissionOption.model_validate(o) for o in params["options"]],
            )
            if session.cancelled.is_set():
                return PromptResponse(stop_reason="cancelled")
            outcome = response.outcome
            if not (isinstance(outcome, AllowedOutcome) and outcome.option_id.startswith("allow")):
                declined = rec.update("permission-reject", "tool_call_update")
                declined["toolCallId"] = tool_call_id
                await self._emit(session_id, declined)
                return await self._reply(session_id, "DENIED")

        if match := re.match(r"sleep (\d+)", command):
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.cancelled.wait(), timeout=int(match.group(1)))
            if session.cancelled.is_set():
                return PromptResponse(stop_reason="cancelled")
            output = "finished"
        else:
            output = command.removeprefix("echo ").strip()
        await self._emit(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": "completed",
            },
        )
        return await self._reply(session_id, output)

    async def _reply(self, session_id: str, text: str) -> PromptResponse:
        self._sessions[session_id].history.append(("agent", text))
        await self._conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=text),
            ),
        )
        await self._emit(session_id, rec.update("ping", "usage_update", 0))
        return PromptResponse.model_validate(rec.result_of("ping", "session/prompt"))

    # ---------------------------------------------------------------- helpers

    async def _emit(self, session_id: str, raw_update: dict[str, Any]) -> None:
        await self._conn.session_update(
            session_id=session_id, update=rec.parse_update(session_id, raw_update)
        )

    def _session(self, session_id: str, *, attached: bool = True) -> FakeSession:
        if session_id not in self._sessions or (attached and session_id not in self._attached):
            raise RequestError.resource_not_found(session_id)
        return self._sessions[session_id]


class SecretKeyGuard:
    """ASGI middleware rejecting requests without the right X-Secret-Key, like goose serve."""

    def __init__(self, app: Any, secret: str) -> None:
        self._app = app
        self._secret = secret.encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            if headers.get(b"x-secret-key") != self._secret:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 401, "headers": []})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self._app(scope, receive, send)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class FakeGooseServer:
    """Runs the mock agent with uvicorn in a background thread.

    Pass ``port`` and ``sessions`` of a previous server to simulate a restart
    of `goose serve` that keeps its stored sessions.
    """

    secret: str
    certfile: str | None = None
    keyfile: str | None = None
    port: int = 0
    sessions: dict[str, FakeSession] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    agents: list[FakeGooseAgent] = field(default_factory=list)

    def __post_init__(self) -> None:
        def factory(conn: Any) -> FakeGooseAgent:
            agent = FakeGooseAgent(conn, self.sessions, self.log)
            self.agents.append(agent)
            return agent

        app = SecretKeyGuard(create_asgi_app(factory), self.secret)
        self.port = self.port or free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            ssl_certfile=self.certfile,
            ssl_keyfile=self.keyfile,
            ws="websockets-sansio",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def url(self, scheme: str) -> str:
        return f"{scheme}://127.0.0.1:{self.port}/acp"

    def start(self) -> FakeGooseServer:
        self._thread.start()
        for _ in range(250):
            if self._server.started:
                return self
            threading.Event().wait(0.02)
        raise RuntimeError("fake goose server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def __enter__(self) -> FakeGooseServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
