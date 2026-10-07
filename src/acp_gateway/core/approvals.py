"""In-memory permission RPCs, human decisions, deadlines and durable audit."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from acp_gateway.agents.client import PermissionRequest
from acp_gateway.channels.base import Channel
from acp_gateway.core.bus import ApprovalRequested, ApprovalResolved, EventBus
from acp_gateway.core.errors import ApprovalNotFound, PolicyDenied
from acp_gateway.core.policy import Policy
from acp_gateway.log import get_logger
from acp_gateway.storage import ApprovalAudit, Conversation, Store
from acp_gateway.storage.records import utcnow


@dataclass(frozen=True)
class Approval:
    id: str
    job_id: str
    conversation: Conversation
    request: PermissionRequest
    requested_at: datetime
    expires_at: datetime
    approver_channels: tuple[str, ...]


@dataclass(frozen=True)
class ApprovalResult:
    option_id: str | None
    outcome: str
    reason: str | None = None


@dataclass
class _Pending:
    approval: Approval
    future: asyncio.Future[ApprovalResult]
    deadline: float


class ApprovalManager:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        policy: Policy,
        channels: Callable[[], Mapping[str, Channel]],
    ) -> None:
        self._store, self._bus, self._policy, self._channels = store, bus, policy, channels
        self._pending: dict[str, _Pending] = {}
        self._closed = False
        self._log = get_logger(__name__)

    def _eligible(self, name: str) -> bool:
        channel = self._channels().get(name)
        return channel is not None and self._policy.can_approve(channel)

    def pending(self, channel: str) -> list[Approval]:
        if not self._eligible(channel):
            raise PolicyDenied("this channel is not a connected human approver")
        return [
            self._view(p.approval, channel)
            for p in self._pending.values()
            if channel in p.approval.approver_channels
        ]

    def _view(self, approval: Approval, channel: str) -> Approval:
        options = tuple(
            o for o in approval.request.options if self._policy.option_allowed(o.kind, channel)
        )
        return replace(
            approval,
            request=replace(
                approval.request, options=options, raw_input=deepcopy(approval.request.raw_input)
            ),
        )

    async def request(
        self, conversation: Conversation, job_id: str, request: PermissionRequest
    ) -> ApprovalResult:
        now = utcnow()
        timeout = self._policy.settings.approval_timeout_seconds
        channels = tuple(name for name in self._channels() if self._eligible(name))
        approval = Approval(
            secrets.token_hex(12),
            job_id,
            conversation,
            deepcopy(request),
            now,
            now + timedelta(seconds=timeout),
            channels,
        )
        loop = asyncio.get_running_loop()
        pending = _Pending(approval, loop.create_future(), loop.time() + timeout)
        self._pending[approval.id] = pending
        if self._closed:
            return self._finish(pending, None, "cancelled", "the gateway is stopping")
        if not self._policy.settings.allow_approvals:
            return self._reject(pending, "policy_denied", "approvals are disabled by policy")
        if not channels:
            return self._reject(pending, "unavailable", "no human approver is connected")
        if not any(self._view(approval, name).request.options for name in channels):
            return self._reject(pending, "policy_denied", "the agent offers no permitted options")
        for name in channels:
            self._bus.publish(ApprovalRequested(conversation, self._view(approval, name), name))
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout)
        except TimeoutError:
            return self._reject(pending, "timed_out", "approval timed out")
        except asyncio.CancelledError:
            if approval.id in self._pending:
                self._finish(pending, None, "cancelled", "the turn was cancelled")
            raise

    def resolve(self, approval_id: str, option_id: str, *, channel: str, actor: str) -> None:
        if not actor.strip() or not self._eligible(channel):
            raise PolicyDenied("an authenticated human in a connected approver channel is required")
        pending = self._pending.get(approval_id)
        if pending is None:
            raise ApprovalNotFound("approval not found or already settled")
        if channel not in pending.approval.approver_channels:
            raise PolicyDenied("this approval was not routed to this channel")
        if asyncio.get_running_loop().time() >= pending.deadline:
            self._reject(pending, "timed_out", "approval timed out")
            raise ApprovalNotFound("approval expired")
        option = next(
            (o for o in pending.approval.request.options if o.option_id == option_id), None
        )
        if option is None or not self._policy.option_allowed(option.kind, channel):
            raise PolicyDenied("this approval option is not permitted")
        self._finish(
            pending,
            option_id,
            "approved" if option.kind.startswith("allow") else "rejected",
            channel=channel,
            actor=actor,
        )

    def _reject(self, pending: _Pending, outcome: str, reason: str) -> ApprovalResult:
        option = pending.approval.request.option("reject_once")
        return self._finish(pending, option.option_id if option else None, outcome, reason)

    def _finish(
        self,
        pending: _Pending,
        option_id: str | None,
        outcome: str,
        reason: str | None = None,
        *,
        channel: str | None = None,
        actor: str | None = None,
    ) -> ApprovalResult:
        approval, req = pending.approval, pending.approval.request
        if approval.id not in self._pending:
            # A deadline callback and a human decision can become ready together.
            # The first settled decision wins and is audited exactly once.
            if pending.future.done() and not pending.future.cancelled():
                return pending.future.result()
            raise ApprovalNotFound("approval not found or already settled")
        result = ApprovalResult(option_id, outcome, reason)
        try:
            # If audit persistence fails, no allow response may reach the agent.
            self._store.record_approval(
                ApprovalAudit(
                    id=approval.id,
                    job_id=approval.job_id,
                    conversation=approval.conversation,
                    acp_session_id=req.session_id,
                    tool_call_id=req.tool_call_id,
                    title=req.title,
                    kind=req.kind,
                    raw_input=req.raw_input,
                    requested_at=approval.requested_at,
                    resolved_at=utcnow(),
                    outcome=outcome,
                    option_id=option_id,
                    decided_channel=channel,
                    actor=actor,
                    reason=reason,
                )
            )
        except Exception:
            pending.future.cancel()
            raise
        finally:
            self._pending.pop(approval.id, None)
        if not pending.future.done():
            pending.future.set_result(result)
        self._bus.publish(
            ApprovalResolved(
                approval.conversation,
                approval.id,
                approval.job_id,
                outcome,
                option_id,
                channel,
                actor,
                reason,
                approval.approver_channels,
            )
        )
        return result

    def cancel_job(self, job_id: str) -> None:
        for pending in list(self._pending.values()):
            if pending.approval.job_id == job_id:
                try:
                    self._finish(pending, None, "cancelled", "the turn was cancelled")
                except Exception:
                    self._log.exception("could not audit cancelled approval")

    def close(self) -> None:
        self._closed = True
        for pending in list(self._pending.values()):
            try:
                self._finish(pending, None, "cancelled", "the gateway stopped")
            except Exception:
                self._log.exception("could not audit approval on shutdown")
