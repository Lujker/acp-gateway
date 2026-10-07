"""Records stored by the gateway; the core and channels use these types directly."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_CHANNEL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_CONVERSATION_KEY = 256


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@dataclass(frozen=True)
class Conversation:
    """One conversation of a channel with one agent.

    ``key`` is the channel's own identifier: a Telegram chat id, a Hermes
    ``thread``, ``default`` for the CLI.
    """

    channel: str
    key: str
    agent: str

    def __post_init__(self) -> None:
        if not _CHANNEL_NAME.match(self.channel):
            raise ValueError(f"invalid channel name {self.channel!r}")
        if not self.key or len(self.key) > MAX_CONVERSATION_KEY:
            raise ValueError(f"conversation key must be 1..{MAX_CONVERSATION_KEY} characters")
        if not self.agent:
            raise ValueError("conversation needs an agent alias")

    def __str__(self) -> str:
        return f"{self.channel}:{self.key}@{self.agent}"


@dataclass(frozen=True)
class SessionRecord:
    """An agent session owned by a conversation."""

    id: int
    conversation: Conversation
    acp_session_id: str
    cwd: str
    title: str | None
    created_at: datetime
    last_used_at: datetime


class JobStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"  # the agent ended the turn (any stop reason except cancelled)
    CANCELLED = "cancelled"
    FAILED = "failed"  # agent error: unavailable, disconnected, prompt failed
    INTERRUPTED = "interrupted"  # the gateway stopped while the turn was running

    @property
    def finished(self) -> bool:
        return self is not JobStatus.RUNNING


@dataclass(frozen=True)
class Job:
    """One prompt turn. ``answer`` is partial while the job is running."""

    id: str
    session_id: int
    conversation: Conversation
    acp_session_id: str
    status: JobStatus
    created_at: datetime
    answer: str = ""
    stop_reason: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = field(default=None, compare=False)
    finished_at: datetime | None = None


@dataclass(frozen=True)
class ApprovalAudit:
    id: str
    job_id: str | None
    conversation: Conversation
    acp_session_id: str
    tool_call_id: str
    title: str | None
    kind: str | None
    raw_input: Any
    requested_at: datetime
    resolved_at: datetime
    outcome: str
    option_id: str | None = None
    decided_channel: str | None = None
    actor: str | None = None
    reason: str | None = None
