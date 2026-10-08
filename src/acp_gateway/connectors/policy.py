"""Local restrictions on dispatcher-to-agent ACP traffic.

The dispatcher is trusted to supply human decisions, but cannot add MCP
servers, advertise client capabilities, select arbitrary directories or put
an approval-mode agent into an unconfigured mode. This is an ACP boundary,
not a filesystem or tool sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from acp_gateway.agents.errors import AgentError
from acp_gateway.agents.transport import AgentTransport
from acp_gateway.config import AgentProfile

MAX_PENDING = 64
MAX_SESSIONS = 1024


class LocalPolicyError(AgentError):
    """A local restriction failed; the message never includes rejected input."""


def _text(value: Any, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise LocalPolicyError("invalid ACP field")
    return value


def _cwd(value: Any) -> str:
    value = _text(value)
    path = PurePosixPath(value)
    if not value.startswith("/") or value.startswith("//") or ".." in path.parts or "\\" in value:
        raise LocalPolicyError("cwd must be an absolute POSIX path without traversal")
    return str(path)


def _id(value: Any) -> int | str:
    if type(value) is int and 0 <= value <= 2**53 - 1:
        return value
    if isinstance(value, str) and value and len(value) <= 128:
        return value
    raise LocalPolicyError("invalid ACP request id")


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocalPolicyError("invalid ACP object")
    return value


@dataclass(frozen=True)
class LocalAgentPolicy:
    """Exact agent-side directories selected locally, never by the dispatcher."""

    allowed_cwds: tuple[str, ...]
    session_mode: str | None = None
    max_prompt_length: int = 50_000

    def __post_init__(self) -> None:
        if not self.allowed_cwds or self.max_prompt_length <= 0:
            raise LocalPolicyError("local policy requires a cwd and a positive prompt limit")
        object.__setattr__(self, "allowed_cwds", tuple(_cwd(c) for c in self.allowed_cwds))
        if self.session_mode is not None:
            _text(self.session_mode, 256)

    @classmethod
    def from_profile(cls, profile: AgentProfile, *, max_prompt_length: int = 50_000):
        return cls((profile.default_cwd,), profile.session_mode, max_prompt_length)

    def prepare(self, message: dict[str, Any]) -> dict[str, Any]:
        """Build a new restricted request; unsupported extension fields are omitted."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise LocalPolicyError("invalid ACP request")
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict) or not isinstance(method, str):
            raise LocalPolicyError("invalid ACP request")
        result: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if method == "session/cancel":
            if "id" in message:
                raise LocalPolicyError("cancel must be a notification")
        else:
            result["id"] = _id(message.get("id"))
        clean: dict[str, Any] = {}
        if method == "initialize":
            version = params.get("protocolVersion")
            if type(version) is not int or not 0 <= version <= 65535:
                raise LocalPolicyError("invalid ACP protocol version")
            clean = {
                "protocolVersion": version,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            }
        elif method in {"session/new", "session/load", "session/list"}:
            cwd = _cwd(self.allowed_cwds[0] if params.get("cwd") is None else params["cwd"])
            if cwd not in self.allowed_cwds:
                raise LocalPolicyError("cwd is not allowed by local policy")
            clean["cwd"] = cwd
            if method != "session/list":
                clean["mcpServers"] = []
                clean["additionalDirectories"] = []
            if method == "session/load":
                clean["sessionId"] = _text(params.get("sessionId"))
            if method == "session/list" and params.get("cursor") is not None:
                clean["cursor"] = _text(params["cursor"])
        elif method in {"session/prompt", "session/cancel", "session/set_mode"}:
            clean["sessionId"] = _text(params.get("sessionId"))
            if method == "session/set_mode":
                if self.session_mode is None or params.get("modeId") != self.session_mode:
                    raise LocalPolicyError("session mode is not allowed by local policy")
                clean["modeId"] = self.session_mode
            elif method == "session/prompt":
                prompt = params.get("prompt")
                if not isinstance(prompt, list) or not 1 <= len(prompt) <= 100:
                    raise LocalPolicyError("invalid text prompt")
                blocks = []
                length = 0
                for block in prompt:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        raise LocalPolicyError("only text prompts are allowed by local policy")
                    text = block.get("text")
                    if not isinstance(text, str):
                        raise LocalPolicyError("invalid text prompt")
                    length += len(text)
                    if length > self.max_prompt_length:
                        raise LocalPolicyError("prompt exceeds local limit")
                    blocks.append({"type": "text", "text": text})
                clean["prompt"] = blocks
        else:
            raise LocalPolicyError("ACP method is not allowed by local policy")
        result["params"] = clean
        return result


@dataclass
class _Permission:
    session_id: str
    options: set[str]


class PolicyTransport:
    """Guard a single local agent stream before attaching it to a relay.

    Permission responses must match a live request and a once-only option.
    A configured session mode must be confirmed before forwarding a prompt.
    A violation closes this stream and fails all in-flight work without replay.
    """

    def __init__(self, transport: AgentTransport, policy: LocalAgentPolicy):
        self._transport = transport
        self.policy = policy
        self.closed = transport.closed
        self._pending: dict[int | str, dict[str, Any]] = {}
        self._permissions: dict[int | str, _Permission] = {}
        self._sessions: dict[str, bool] = {}

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed.is_set():
            raise ConnectionError("agent stream closed")
        try:
            message = _object(message)
            if "method" not in message:
                clean = self._permission_response(message)
            else:
                clean = self.policy.prepare(message)
                method = clean["method"]
                params = clean["params"]
                session = params.get("sessionId")
                if method in {"session/prompt", "session/set_mode", "session/cancel"}:
                    if session not in self._sessions:
                        raise LocalPolicyError("session is not attached to this stream")
                    if method == "session/prompt" and not self._sessions[session]:
                        raise LocalPolicyError("configured session mode has not been confirmed")
                    if method == "session/prompt" and any(
                        r["method"] == method and r["params"]["sessionId"] == session
                        for r in self._pending.values()
                    ):
                        raise LocalPolicyError("session already has an active prompt")
                    if method == "session/set_mode":
                        self._sessions[session] = False
                    if method == "session/cancel":
                        self._invalidate_permissions(session)
                if method == "session/load" and session in self._sessions:
                    self._sessions[session] = False
                    self._invalidate_permissions(session)
                if "id" in clean:
                    request_id = clean["id"]
                    if request_id in self._pending or len(self._pending) >= MAX_PENDING:
                        raise LocalPolicyError("too many or duplicate ACP requests")
                    self._pending[request_id] = clean
            await self._transport.send(clean)
        except LocalPolicyError:
            await self.close()
            raise

    def _permission_response(self, message):
        if message.get("jsonrpc") != "2.0" or "error" in message:
            raise LocalPolicyError("invalid permission response")
        request_id = _id(message.get("id"))
        permission = self._permissions.get(request_id)
        if permission is None:
            raise LocalPolicyError("permission request is no longer pending")
        result = message.get("result")
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if not isinstance(outcome, dict):
            raise LocalPolicyError("invalid permission response")
        if outcome.get("outcome") == "cancelled":
            clean = {"outcome": "cancelled"}
        elif (
            outcome.get("outcome") == "selected"
            and isinstance(outcome.get("optionId"), str)
            and outcome["optionId"] in permission.options
        ):
            clean = {"outcome": "selected", "optionId": outcome["optionId"]}
        else:
            raise LocalPolicyError("permission option is not allowed by local policy")
        del self._permissions[request_id]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"outcome": clean}}

    async def receive(self) -> dict[str, Any] | None:
        try:
            while (message := await self._transport.receive()) is not None:
                message = _object(message)
                if message.get("jsonrpc") != "2.0":
                    raise LocalPolicyError("invalid agent ACP message")
                method = message.get("method")
                if method is not None and not isinstance(method, str):
                    raise LocalPolicyError("invalid agent ACP method")
                if method == "session/request_permission":
                    request_id = _id(message.get("id"))
                    params = message.get("params")
                    if not isinstance(params, dict) or not isinstance(params.get("options"), list):
                        raise LocalPolicyError("invalid agent permission request")
                    session = _text(params.get("sessionId"))
                    if not self._sessions.get(session) or not any(
                        r["method"] == "session/prompt" and r["params"]["sessionId"] == session
                        for r in self._pending.values()
                    ):
                        raise LocalPolicyError("permission has no active prompt")
                    if len(params["options"]) > 100:
                        raise LocalPolicyError("too many permission options")
                    all_ids = [_text(_object(o).get("optionId")) for o in params["options"]]
                    if len(all_ids) != len(set(all_ids)):
                        raise LocalPolicyError("duplicate permission options")
                    if request_id in self._permissions or len(self._permissions) >= MAX_PENDING:
                        raise LocalPolicyError("too many or duplicate permission requests")
                    options = [
                        {
                            "optionId": _text(o.get("optionId")),
                            "name": _text(o.get("name")),
                            "kind": o["kind"],
                        }
                        for o in params["options"]
                        if isinstance(o.get("kind"), str)
                        and o["kind"] in {"allow_once", "reject_once"}
                    ]
                    self._permissions[request_id] = _Permission(
                        session, {o["optionId"] for o in options}
                    )
                    return {**message, "params": {**params, "options": options}}
                if method is not None and "id" in message:
                    # Never forward fs/terminal/other client requests to the VPS.
                    await self._transport.send(
                        {
                            "jsonrpc": "2.0",
                            "id": _id(message["id"]),
                            "error": {"code": -32601, "message": "Client method is not supported"},
                        }
                    )
                    continue
                if method == "session/update":
                    params = _object(message.get("params"))
                    update = _object(params.get("update"))
                    if (
                        self.policy.session_mode
                        and update.get("sessionUpdate") == "current_mode_update"
                    ):
                        session = _text(params.get("sessionId"))
                        if session in self._sessions:
                            self._sessions[session] = (
                                update.get("currentModeId") == self.policy.session_mode
                            )
                            if not self._sessions[session]:
                                self._invalidate_permissions(session)
                elif method is None and "id" in message:
                    request = self._pending.pop(_id(message["id"]), None)
                    if request is not None and request["method"] == "session/prompt":
                        self._invalidate_permissions(request["params"]["sessionId"])
                    if request is not None and "result" in message and "error" not in message:
                        self._on_result(request, message["result"])
                return message
            return None
        except LocalPolicyError:
            await self.close()
            raise

    def _on_result(self, request, result):
        if not isinstance(result, dict):
            return
        method = request["method"]
        if method in {"session/new", "session/load"}:
            session = (
                result.get("sessionId")
                if method == "session/new"
                else request["params"]["sessionId"]
            )
            session = _text(session)
            if session not in self._sessions and len(self._sessions) >= MAX_SESSIONS:
                raise LocalPolicyError("too many attached sessions")
            modes = result.get("modes")
            self._sessions[session] = self.policy.session_mode is None or (
                isinstance(modes, dict) and modes.get("currentModeId") == self.policy.session_mode
            )
        elif method == "session/set_mode":
            self._sessions[request["params"]["sessionId"]] = True

    def _invalidate_permissions(self, session_id: str) -> None:
        for permission in self._permissions.values():
            if permission.session_id == session_id:
                permission.options.clear()

    async def close(self) -> None:
        self._pending.clear()
        self._permissions.clear()
        self._sessions.clear()
        await self._transport.close()
