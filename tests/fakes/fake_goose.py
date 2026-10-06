"""A fake ACP agent served over WebSocket/HTTP that imitates `goose serve`.

Imitated: ``X-Secret-Key`` auth, optional TLS with a self-signed certificate,
session modes, ``session/request_permission`` for shell commands, cancel,
``session/load`` with history replay. Behaviour is keyword-driven by the prompt
text, matching the prompts used by ``scripts/spike_acp.py``.

This is a scaffold for the P1.2 mock agent; once real Work Goose traffic is
recorded, the mock should replay it instead of guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from acp.http.asgi import create_asgi_app
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AllowedOutcome,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    SessionMode,
    SessionModeState,
    SetSessionModeResponse,
    TextContentBlock,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
)

MODES = [
    SessionMode(id="auto", name="Autonomous"),
    SessionMode(id="approve", name="Manual approval"),
    SessionMode(id="chat", name="Chat only"),
]
PERMISSION_OPTIONS = [
    PermissionOption(option_id="allow_once", name="Allow once", kind="allow_once"),
    PermissionOption(option_id="allow_always", name="Always allow", kind="allow_always"),
    PermissionOption(option_id="reject_once", name="Deny", kind="reject_once"),
]


@dataclass
class FakeSession:
    cwd: str
    mode: str = "approve"
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    memory: dict[str, str] = field(default_factory=dict)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class FakeGooseAgent:
    def __init__(self, conn: Any, sessions: dict[str, FakeSession], log: list[str]) -> None:
        self._conn = conn
        self._sessions = sessions
        self._log = log
        self.client_capabilities: Any = None

    async def initialize(self, protocol_version: int, client_capabilities=None, **kw: Any):
        self.client_capabilities = client_capabilities
        self._log.append("initialize")
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(load_session=True),
        )

    async def new_session(self, cwd: str, mcp_servers=None, **kw: Any):
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = FakeSession(cwd=cwd)
        self._log.append(f"new_session mcp_servers={len(mcp_servers or [])}")
        return NewSessionResponse(session_id=session_id, modes=self._modes("approve"))

    async def load_session(self, cwd: str, session_id: str, mcp_servers=None, **kw: Any):
        session = self._sessions[session_id]
        for role, text in session.history:
            chunk_cls = UserMessageChunk if role == "user" else AgentMessageChunk
            update = chunk_cls(
                session_update="user_message_chunk" if role == "user" else "agent_message_chunk",
                content=TextContentBlock(type="text", text=text),
            )
            await self._conn.session_update(session_id=session_id, update=update)
        return LoadSessionResponse(modes=self._modes(session.mode))

    async def set_session_mode(self, session_id: str, mode_id: str, **kw: Any):
        self._sessions[session_id].mode = mode_id
        return SetSessionModeResponse()

    async def cancel(self, session_id: str, **kw: Any) -> None:
        self._log.append("cancel")
        self._sessions[session_id].cancelled.set()

    async def prompt(self, session_id: str, prompt: list[Any], **kw: Any):
        session = self._sessions[session_id]
        session.cancelled.clear()
        text = " ".join(getattr(block, "text", "") for block in prompt)
        session.history.append(("user", text))

        if match := re.search(r"run exactly(?: this command)?: (.+?) \.", text):
            command = match.group(1)
            if session.mode != "auto":
                allowed = await self._ask_permission(session_id, command)
                if session.cancelled.is_set():
                    return PromptResponse(stop_reason="cancelled")
                if not allowed:
                    return await self._reply(session_id, "DENIED")
            return await self._run(session_id, command)

        if match := re.search(r"code word (\w+)", text):
            session.memory["word"] = match.group(1)
            return await self._reply(session_id, "OK")
        if "What was the code word" in text:
            return await self._reply(session_id, session.memory.get("word", "unknown"))
        if "pong" in text:
            return await self._reply(session_id, "pong")
        return await self._reply(session_id, f"echo: {text}")

    async def _ask_permission(self, session_id: str, command: str) -> bool:
        tool_call_id = uuid.uuid4().hex[:8]
        await self._conn.session_update(
            session_id=session_id,
            update=ToolCallStart(
                session_update="tool_call",
                tool_call_id=tool_call_id,
                title="shell",
                kind="execute",
                status="pending",
                raw_input={"command": command},
            ),
        )
        response = await self._conn.request_permission(
            session_id=session_id,
            tool_call=ToolCallUpdate(
                tool_call_id=tool_call_id,
                title="shell",
                kind="execute",
                raw_input={"command": command},
            ),
            options=PERMISSION_OPTIONS,
        )
        outcome = response.outcome
        return isinstance(outcome, AllowedOutcome) and outcome.option_id.startswith("allow")

    async def _run(self, session_id: str, command: str) -> PromptResponse:
        session = self._sessions[session_id]
        if match := re.match(r"sleep (\d+)", command):
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.cancelled.wait(), timeout=int(match.group(1)))
            if session.cancelled.is_set():
                return PromptResponse(stop_reason="cancelled")
            return await self._reply(session_id, "finished")
        return await self._reply(session_id, command.removeprefix("echo ").strip())

    async def _reply(self, session_id: str, text: str) -> PromptResponse:
        self._sessions[session_id].history.append(("agent", text))
        await self._conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=text),
            ),
        )
        return PromptResponse(stop_reason="end_turn")

    def _modes(self, current: str) -> SessionModeState:
        return SessionModeState(current_mode_id=current, available_modes=MODES)


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


@dataclass
class FakeGooseServer:
    """Runs the fake agent with uvicorn in a background thread."""

    secret: str
    certfile: str | None = None
    keyfile: str | None = None
    sessions: dict[str, FakeSession] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    agents: list[FakeGooseAgent] = field(default_factory=list)
    port: int = 0

    def __post_init__(self) -> None:
        def factory(conn: Any) -> FakeGooseAgent:
            agent = FakeGooseAgent(conn, self.sessions, self.log)
            self.agents.append(agent)
            return agent

        app = SecretKeyGuard(create_asgi_app(factory), self.secret)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
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

    def __enter__(self) -> FakeGooseServer:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            threading.Event().wait(0.02)
        raise RuntimeError("fake goose server did not start")

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
