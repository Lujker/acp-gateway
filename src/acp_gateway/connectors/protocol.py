"""Bounded, versioned control frames with input-independent errors."""

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

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


class _Frame(_Model):
    version: Literal[1] = 1


class Hello(_Frame):
    type: Literal["hello"] = "hello"
    computer_id: ComputerId
    agents: Annotated[list[AgentManifest], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_aliases(self):
        if len({agent.alias for agent in self.agents}) != len(self.agents):
            raise ValueError("duplicate agent aliases")
        return self


class Welcome(_Frame):
    type: Literal["welcome"] = "welcome"
    connection_id: UUID
    heartbeat_seconds: Annotated[int, Field(ge=5, le=300)] = 20


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


class Data(_StreamFrame):
    type: Literal["data"] = "data"
    message: dict[str, Any]


class Close(_StreamFrame):
    type: Literal["close"] = "close"
    code: Literal["closed", "unavailable", "access_denied", "policy_denied", "overflow"] = "closed"


ControlFrame = Hello | Welcome | Ping | Pong | Error | Open | Opened | Data | Close
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
        if value["version"] != 1:
            raise ProtocolError("unsupported protocol version")
        if value.get("type") != "data" and len(encoded) > MAX_FRAME_BYTES:
            raise ProtocolError("control frame too large")
        return _ADAPTER.validate_json(json.dumps(value, allow_nan=False))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid control frame") from None


def encode_frame(frame: ControlFrame) -> str:
    return decode_frame(frame.model_dump_json()).model_dump_json()
