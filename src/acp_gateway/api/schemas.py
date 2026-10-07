"""Request bodies for the owner API; actor identities are never caller-supplied."""

from pydantic import BaseModel, ConfigDict, Field


class ConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    thread: str = Field(default="default", min_length=1, max_length=256)


class NewSessionRequest(ConversationRequest):
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)


class PromptRequest(ConversationRequest):
    text: str
    wait: float = Field(default=0, ge=0, le=300, allow_inf_nan=False)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str = Field(min_length=1, max_length=256)
    lease_id: str = Field(min_length=1, max_length=256)
