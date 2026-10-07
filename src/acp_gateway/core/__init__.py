"""The gateway core: sessions, jobs and the event bus shared by every channel."""

from acp_gateway.core.approvals import Approval, ApprovalManager
from acp_gateway.core.bus import (
    ApprovalRequested,
    ApprovalResolved,
    EventBus,
    GatewayEvent,
    JobFinished,
    JobProgress,
    JobStarted,
    SessionCreated,
    Subscription,
    for_approver,
    for_channel,
    for_conversation,
)
from acp_gateway.core.errors import (
    ApprovalNotFound,
    GatewayError,
    JobNotFound,
    PolicyDenied,
    UnknownAgent,
    UnknownSession,
)
from acp_gateway.core.gateway import GatewayCore
from acp_gateway.storage.records import Conversation, Job, JobStatus, SessionRecord

__all__ = [
    "Approval",
    "ApprovalManager",
    "ApprovalNotFound",
    "ApprovalRequested",
    "ApprovalResolved",
    "Conversation",
    "EventBus",
    "GatewayCore",
    "GatewayError",
    "GatewayEvent",
    "Job",
    "JobFinished",
    "JobNotFound",
    "JobProgress",
    "JobStarted",
    "JobStatus",
    "PolicyDenied",
    "SessionCreated",
    "SessionRecord",
    "Subscription",
    "UnknownAgent",
    "UnknownSession",
    "for_approver",
    "for_channel",
    "for_conversation",
]
