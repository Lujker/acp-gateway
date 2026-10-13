"""Request bodies for the owner API; actor identities are never caller-supplied."""

from pydantic import BaseModel, ConfigDict, Field


class ConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str | None = Field(
        default=None, pattern=r"^(?:[a-z][a-z0-9_-]{0,63}/)?[a-z][a-z0-9_]{0,31}$"
    )
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


class MaintenanceResume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    force: bool = Field(default=False, strict=True)


class ModelSelectionRequest(ConversationRequest):
    provider: str = Field(max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@+~-]*$")
    model: str = Field(max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@+~-]*$")
    expected_revision: int = Field(ge=0, strict=True)
    confirmation_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class ModelDefaultRequest(ConversationRequest):
    provider: str = Field(max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@+~-]*$")
    model: str = Field(max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@+~-]*$")


class ModelReconcileRequest(ConversationRequest):
    expected_revision: int = Field(ge=0, strict=True)
