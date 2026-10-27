"""Bounded, versioned control frames with input-independent errors."""

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

MAX_FRAME_BYTES = 65_536
MAX_DATA_BYTES = 1_048_576
ComputerId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
AgentAlias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class ProtocolError(ValueError):
    """Safe to log: never contains rejected input."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentManifest(_Model):
    alias: AgentAlias
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Literal["goose", "generic", "hermes", "codex", "claude_code"] | None = None
    default_cwd: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    session_mode: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class _Frame(_Model):
    version: Literal[1] = 1


class Hello(_Frame):
    version: Literal[1, 2] = 1
    type: Literal["hello"] = "hello"
    computer_id: ComputerId
    agents: Annotated[list[AgentManifest], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_aliases(self):
        if len({agent.alias for agent in self.agents}) != len(self.agents):
            raise ValueError("duplicate agent aliases")
        if self.version == 1 and any(
            a.kind is not None or a.default_cwd is not None or a.session_mode is not None
            for a in self.agents
        ):
            raise ValueError("agent metadata requires version 2")
        if self.version == 2:
            from acp_gateway.connectors.policy import LocalPolicyError, _cwd

            for agent in self.agents:
                if agent.kind is None or agent.default_cwd is None:
                    raise ValueError("version 2 requires local agent metadata")
                try:
                    _cwd(agent.default_cwd)
                except LocalPolicyError:
                    raise ValueError("invalid agent working directory") from None
        return self


class Welcome(_Frame):
    type: Literal["welcome"] = "welcome"
    connection_id: UUID
    heartbeat_seconds: Annotated[int, Field(ge=5, le=300)] = 20
    update_inventory: Literal[1] | None = None
    update_operations: Literal[1] | None = None
    agent_profiles: Literal[1] | None = None
    agent_discovery: Literal[1] | None = None
    stream_resume: Literal[1] | None = None


class ProfileSpec(_Model):
    alias: AgentAlias
    template: AgentAlias | None = None
    candidate: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    title: Annotated[str, Field(min_length=1, max_length=128)]
    cwd: Annotated[str, Field(min_length=1, max_length=4096)]
    mode: Literal["safe", "work"]

    @model_validator(mode="after")
    def one_source(self):
        if (self.template is None) == (self.candidate is None):
            raise ValueError("select one local source")
        return self


class AgentCandidate(_Model):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    kind: Literal["codex", "claude_code"]
    title: Annotated[str, Field(min_length=1, max_length=128)]
    default_cwd: Annotated[str, Field(min_length=1, max_length=4096)]


class ProfilesRequest(_Frame):
    type: Literal["agent_profiles"] = "agent_profiles"
    epoch: UUID
    request: UUID
    operation: UUID
    action: Literal["list", "discover", "add", "update", "remove"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    actor: Annotated[str, Field(pattern=r"^telegram:[0-9]{1,20}$")]
    expires: Annotated[float, Field(allow_inf_nan=False)]
    alias: AgentAlias | None = None
    profile: ProfileSpec | None = None


class ProfilesResult(_Model):
    status: Literal["ok", "disabled", "busy", "stale", "invalid", "unknown"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    profiles: Annotated[list[ProfileSpec], Field(max_length=100)] = Field(default_factory=list)
    agents: Annotated[list[AgentManifest], Field(max_length=100)] = Field(default_factory=list)
    candidates: Annotated[list[AgentCandidate], Field(max_length=32)] | None = None
    hints: Annotated[list[Annotated[str, Field(max_length=256)]], Field(max_length=32)] | None = (
        None
    )
    initialization: Literal["ok", "failed"] | None = None


class ProfilesResponse(_Frame):
    type: Literal["agent_profiles_result"] = "agent_profiles_result"
    epoch: UUID
    request: UUID
    result: ProfilesResult


class InstallationReport(_Model):
    installed_version: Annotated[str, Field(pattern=r"^[0-9A-Za-z.+!-]{1,64}$")]
    manager: Literal["uv", "docker", "unmanaged", "binary"]
    source: Literal["registry", "editable", "git", "file"]
    platform: Literal["linux", "win32", "darwin", "other"]
    # Discovery never grants permission to mutate the installation.
    can_update: bool = False

    @field_validator("installed_version")
    @classmethod
    def valid_version(cls, value):
        Version(value)
        return value


class UpdateInfoRequest(_Frame):
    type: Literal["update_info"] = "update_info"
    epoch: UUID
    request: UUID


class UpdateInfoResponse(_Frame):
    type: Literal["update_info_result"] = "update_info_result"
    epoch: UUID
    request: UUID
    report: InstallationReport | None = None


class UpdateOperation(_Model):
    operation: UUID
    status: Literal[
        "awaiting_confirmation",
        "dispatching",
        "queued",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "cancelled",
        "not_found",
    ]
    reason: Literal[
        "",
        "disabled",
        "unsupported",
        "busy",
        "expired",
        "stale",
        "conflict",
        "version_not_allowed",
        "failed",
        "unknown",
        "rolled_back",
        "worker_stopped",
    ] = ""


class UpdateRequest(_Frame):
    type: Literal["update_operation"] = "update_operation"
    epoch: UUID
    request: UUID
    operation: UUID
    action: Literal["execute", "status"]
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    target_version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=64)]
    current_version: Annotated[str, Field(min_length=1, max_length=64)]
    expires: Annotated[float, Field(allow_inf_nan=False)]


class UpdateResponse(_Frame):
    type: Literal["update_operation_result"] = "update_operation_result"
    epoch: UUID
    request: UUID
    result: UpdateOperation


class Ping(_Frame):
    type: Literal["ping"] = "ping"
    sequence: Annotated[int, Field(ge=0, le=2**53 - 1)]


class Pong(_Frame):
    type: Literal["pong"] = "pong"
    sequence: Annotated[int, Field(ge=0, le=2**53 - 1)]


class Error(_Frame):
    type: Literal["error"] = "error"
    code: Literal["invalid_frame", "unsupported_version", "unauthorized", "unavailable"]


class _StreamFrame(_Frame):
    epoch: UUID
    stream: UUID
    alias: AgentAlias


class Open(_StreamFrame):
    type: Literal["open"] = "open"


class Opened(_StreamFrame):
    type: Literal["opened"] = "opened"


RelaySequence = Annotated[int, Field(ge=0, le=2**53 - 1)]


class Data(_StreamFrame):
    type: Literal["data"] = "data"
    # Present only when stream resume is negotiated; starts at 1 per direction.
    seq: Annotated[int, Field(ge=1, le=2**53 - 1)] | None = None
    message: dict[str, Any]


class Ack(_StreamFrame):
    """The sender may forget data up to ``received``."""

    type: Literal["ack"] = "ack"
    received: RelaySequence


class Resume(_StreamFrame):
    """Dispatcher asks to move a parked stream to this connection epoch."""

    type: Literal["resume"] = "resume"
    received: RelaySequence


class Resumed(_StreamFrame):
    """Connector accepted the stream; each side replays data after ``received``."""

    type: Literal["resumed"] = "resumed"
    received: RelaySequence


class Close(_StreamFrame):
    type: Literal["close"] = "close"
    code: Literal["closed", "unavailable", "access_denied", "policy_denied", "overflow"] = "closed"


ControlFrame = (
    Hello
    | Welcome
    | Ping
    | Pong
    | Error
    | Open
    | Opened
    | Data
    | Close
    | Ack
    | Resume
    | Resumed
    | UpdateInfoRequest
    | UpdateInfoResponse
    | UpdateRequest
    | UpdateResponse
    | ProfilesRequest
    | ProfilesResponse
)
_ADAPTER = TypeAdapter(Annotated[ControlFrame, Field(discriminator="type")])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("invalid control frame")
        result[key] = value
    return result


def decode_frame(payload: str | bytes) -> ControlFrame:
    try:
        encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(encoded) > MAX_DATA_BYTES:
            raise ProtocolError("control frame too large")
        value = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ProtocolError("invalid control frame")
        if value["version"] != 1 and not (value["version"] == 2 and value.get("type") == "hello"):
            raise ProtocolError("unsupported protocol version")
        if value.get("type") != "data" and len(encoded) > MAX_FRAME_BYTES:
            raise ProtocolError("control frame too large")
        return _ADAPTER.validate_json(json.dumps(value, allow_nan=False))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid control frame") from None


def encode_frame(frame: ControlFrame) -> str:
    return decode_frame(frame.model_dump_json(exclude_none=True)).model_dump_json(exclude_none=True)
