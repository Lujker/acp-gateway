"""Agent events normalized from ACP ``session/update`` notifications.

The gateway core and channels work with these types only, so ACP schema
details (and its unions) stay inside ``acp_gateway.agents``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    session_id: str


@dataclass(frozen=True)
class MessageChunk(AgentEvent):
    """A piece of the agent's answer."""

    text: str


@dataclass(frozen=True)
class UserMessageChunk(AgentEvent):
    """A piece of a user message; goose sends these when replaying history."""

    text: str


@dataclass(frozen=True)
class ThoughtChunk(AgentEvent):
    text: str


@dataclass(frozen=True)
class ToolCallStarted(AgentEvent):
    tool_call_id: str
    title: str
    kind: str | None = None
    status: str | None = None
    raw_input: Any = None


@dataclass(frozen=True)
class ToolCallUpdated(AgentEvent):
    tool_call_id: str
    status: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class PlanUpdated(AgentEvent):
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModeChanged(AgentEvent):
    mode_id: str


@dataclass(frozen=True)
class UsageUpdated(AgentEvent):
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionInfoUpdated(AgentEvent):
    title: str | None = None


@dataclass(frozen=True)
class CommandsUpdated(AgentEvent):
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class OtherUpdate(AgentEvent):
    """An update kind the gateway does not model (yet)."""

    kind: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnFinished(AgentEvent):
    """Always the last event of a prompt turn."""

    stop_reason: str
    usage: dict[str, Any] | None = None


def _text(update: Any) -> str:
    content = getattr(update, "content", None)
    return (getattr(content, "text", None) or "") if content is not None else ""


def _dump(update: Any) -> dict[str, Any]:
    return update.model_dump(by_alias=True, exclude_none=True, mode="json")


def normalize(session_id: str, update: Any) -> AgentEvent:
    """Convert one ACP session update into a gateway event."""
    kind = getattr(update, "session_update", None) or type(update).__name__
    match kind:
        case "agent_message_chunk":
            return MessageChunk(session_id, _text(update))
        case "user_message_chunk":
            return UserMessageChunk(session_id, _text(update))
        case "agent_thought_chunk":
            return ThoughtChunk(session_id, _text(update))
        case "tool_call":
            return ToolCallStarted(
                session_id,
                tool_call_id=update.tool_call_id,
                title=update.title,
                kind=update.kind,
                status=update.status,
                raw_input=update.raw_input,
            )
        case "tool_call_update":
            return ToolCallUpdated(
                session_id,
                tool_call_id=update.tool_call_id,
                status=update.status,
                title=update.title,
            )
        case "plan" | "plan_update" | "plan_removed":
            return PlanUpdated(session_id, raw=_dump(update))
        case "current_mode_update":
            return ModeChanged(session_id, mode_id=update.current_mode_id)
        case "usage_update":
            return UsageUpdated(session_id, raw=_dump(update))
        case "session_info_update":
            return SessionInfoUpdated(session_id, title=getattr(update, "title", None))
        case "available_commands_update":
            names = tuple(c.name for c in update.available_commands or [])
            return CommandsUpdated(session_id, names=names)
        case _:
            raw = _dump(update) if hasattr(update, "model_dump") else {}
            return OtherUpdate(session_id, kind=str(kind), raw=raw)
