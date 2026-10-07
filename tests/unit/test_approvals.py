"""Approval state transitions and policy at the human/agent boundary."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from acp_gateway.agents import PermissionOption, PermissionRequest
from acp_gateway.channels import Channel
from acp_gateway.config import PolicySettings
from acp_gateway.core import (
    ApprovalManager,
    ApprovalNotFound,
    ApprovalRequested,
    EventBus,
    PolicyDenied,
    for_approver,
)
from acp_gateway.core.policy import Policy
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, JobStatus, Store
from acp_gateway.storage.records import utcnow


class HumanChannel(Channel):
    can_approve = True

    def __init__(self, name="cli"):
        self.name = name
        self.online = True

    @property
    def connected(self):
        return self.online

    async def start(self, core):
        pass

    async def stop(self):
        self.online = False


@pytest.fixture
def harness(tmp_path):
    store = Store.open(tmp_path / "gateway.db")
    conversation = Conversation("mcp", "thread-1", "work")
    session = store.add_session(conversation, "s1", "/work")
    job = store.add_job("job1", session.id)
    bus = EventBus()
    policy = Policy(PolicySettings(approval_timeout_seconds=1))
    channels = {"cli": HumanChannel()}
    manager = ApprovalManager(store, bus, policy, lambda: channels)
    request = PermissionRequest(
        "s1",
        "tool1",
        "shell · echo hello",
        "execute",
        {"command": "echo hello"},
        tuple(PermissionOption(k, k, k) for k in ["allow_once", "reject_once", "allow_always"]),
    )
    yield SimpleNamespace(
        store=store,
        bus=bus,
        policy=policy,
        channels=channels,
        manager=manager,
        request=request,
        conversation=conversation,
        job=job,
    )
    manager.close()
    bus.close()
    store.close()


async def begin(h):
    with h.bus.subscribe(for_approver("cli")) as events:
        task = asyncio.create_task(h.manager.request(h.conversation, h.job.id, h.request))
        event = await asyncio.wait_for(events.get(), 2)
        assert isinstance(event, ApprovalRequested)
        return task, event.approval


@pytest.mark.parametrize(
    "option,outcome", [("allow_once", "approved"), ("reject_once", "rejected")]
)
async def test_human_decision_is_audited_once_and_late_decision_fails(harness, option, outcome):
    h = harness
    task, approval = await begin(h)
    h.manager.resolve(approval.id, option, channel="cli", actor="alice")
    assert (await task).option_id == option
    (audit,) = h.store.approval_audit()
    assert (audit.outcome, audit.actor, audit.decided_channel) == (outcome, "alice", "cli")
    assert audit.conversation == h.conversation
    assert h.manager.pending("cli") == []
    with pytest.raises(ApprovalNotFound):
        h.manager.resolve(approval.id, option, channel="cli", actor="bob")
    assert len(h.store.approval_audit()) == 1


@pytest.mark.parametrize("option", ["allow_always", "unknown"])
async def test_forbidden_option_keeps_request_pending(harness, option):
    task, approval = await begin(harness)
    with pytest.raises(PolicyDenied):
        harness.manager.resolve(approval.id, option, channel="cli", actor="alice")
    assert not task.done()
    assert [o.kind for o in harness.manager.pending("cli")[0].request.options] == [
        "allow_once",
        "reject_once",
    ]
    harness.manager.cancel_job(harness.job.id)
    assert (await task).outcome == "cancelled"


@pytest.mark.parametrize("channel", ["mcp", "hermes"])
async def test_llm_cannot_approve_even_when_misconfigured_as_human(harness, channel):
    h = harness
    h.channels[channel] = HumanChannel(channel)
    h.policy.settings.approver_channels.append(channel)
    task, approval = await begin(h)
    with pytest.raises(PolicyDenied):
        h.manager.resolve(approval.id, "allow_once", channel=channel, actor="model")
    h.manager.cancel_job(h.job.id)
    await task


@pytest.mark.parametrize("reason", ["offline", "not_human", "not_allowed", "disabled"])
async def test_no_eligible_approver_rejects_without_waiting(harness, reason):
    h = harness
    if reason == "offline":
        h.channels["cli"].online = False
    elif reason == "not_human":
        h.channels["cli"].can_approve = False
    elif reason == "not_allowed":
        h.policy.settings.approver_channels.clear()
    else:
        h.policy.settings.allow_approvals = False
    result = await h.manager.request(h.conversation, h.job.id, h.request)
    assert result.option_id == "reject_once"
    assert result.reason
    (audit,) = h.store.approval_audit()
    assert audit.outcome in {"unavailable", "policy_denied"}


async def test_timeout_rejects_and_records_one_decision(harness):
    task, approval = await begin(harness)
    result = await asyncio.wait_for(task, 2)
    assert result.outcome == "timed_out"
    assert result.option_id == "reject_once"
    with pytest.raises(ApprovalNotFound):
        harness.manager.resolve(approval.id, "allow_once", channel="cli", actor="alice")
    assert len(harness.store.approval_audit()) == 1


async def test_decision_after_deadline_cannot_win_timeout_race(harness):
    task, approval = await begin(harness)
    harness.manager._pending[approval.id].deadline = asyncio.get_running_loop().time()
    with pytest.raises(ApprovalNotFound, match="expired"):
        harness.manager.resolve(approval.id, "allow_once", channel="cli", actor="alice")
    assert (await task).outcome == "timed_out"
    assert len(harness.store.approval_audit()) == 1


async def test_disconnected_or_unauthenticated_actor_cannot_decide(harness):
    task, approval = await begin(harness)
    with pytest.raises(PolicyDenied):
        harness.manager.resolve(approval.id, "allow_once", channel="cli", actor=" ")
    harness.channels["cli"].online = False
    with pytest.raises(PolicyDenied):
        harness.manager.resolve(approval.id, "allow_once", channel="cli", actor="alice")
    harness.manager.cancel_job(harness.job.id)
    await task


@pytest.mark.parametrize("operation", ["task_cancel", "job_cancel", "shutdown"])
async def test_cancellation_and_shutdown_settle_pending_once(harness, operation):
    task, _ = await begin(harness)
    if operation == "task_cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if operation == "job_cancel":
            harness.manager.cancel_job(harness.job.id)
        else:
            harness.manager.close()
        assert (await task).option_id is None
    (audit,) = harness.store.approval_audit()
    assert audit.outcome == "cancelled"
    assert harness.manager.pending("cli") == []


async def test_audit_masks_secrets_and_survives_reopen(harness, tmp_path):
    h = harness
    secret = "audit-" + "secret-value"
    register_secret(secret)
    h.request = PermissionRequest(
        "s1",
        "tool1",
        f"echo {secret}",
        "execute",
        {"command": f"echo {secret}", "token": "sensitive", "nested": {"password": "hidden"}},
        h.request.options,
    )
    task, approval = await begin(h)
    h.manager.resolve(approval.id, "reject_once", channel="cli", actor="alice")
    await task
    second = Store.open(tmp_path / "gateway.db")
    try:
        (audit,) = second.approval_audit()
    finally:
        second.close()
    assert audit.title == "echo ***"
    assert audit.raw_input == {"command": "echo ***", "token": "***", "nested": {"password": "***"}}


async def test_audit_failure_never_grants_permission(harness, monkeypatch):
    task, approval = await begin(harness)

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(harness.store, "record_approval", fail)
    with pytest.raises(OSError):
        harness.manager.resolve(approval.id, "allow_once", channel="cli", actor="alice")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.manager.pending("cli") == []


async def test_allow_always_requires_explicit_policy_and_local_cli(harness):
    h = harness
    h.policy.settings.allow_always_approval = True
    h.policy.settings.approver_channels.append("telegram")
    h.channels["telegram"] = HumanChannel("telegram")
    task, approval = await begin(h)
    assert "allow_always" not in [o.kind for o in h.manager.pending("telegram")[0].request.options]
    with pytest.raises(PolicyDenied):
        h.manager.resolve(approval.id, "allow_always", channel="telegram", actor="alice")
    h.manager.resolve(approval.id, "allow_always", channel="cli", actor="alice")
    assert (await task).option_id == "allow_always"


async def test_request_without_reject_option_falls_back_to_cancelled(harness):
    h = harness
    h.channels.clear()
    request = replace(h.request, options=(h.request.options[0],))
    result = await h.manager.request(h.conversation, h.job.id, request)
    assert result.option_id is None
    assert result.outcome == "unavailable"


async def test_channel_added_after_request_cannot_take_ownership(harness):
    task, approval = await begin(harness)
    harness.channels["telegram"] = HumanChannel("telegram")
    harness.policy.settings.approver_channels.append("telegram")
    with pytest.raises(PolicyDenied, match="not routed"):
        harness.manager.resolve(approval.id, "allow_once", channel="telegram", actor="alice")
    harness.manager.cancel_job(harness.job.id)
    await task


async def test_audit_survives_job_retention_cleanup(harness):
    task, approval = await begin(harness)
    harness.manager.resolve(approval.id, "reject_once", channel="cli", actor="alice")
    await task
    harness.store.finish_job(harness.job.id, JobStatus.COMPLETED)
    assert harness.store.prune_jobs(utcnow() + timedelta(days=1)) == 1
    (audit,) = harness.store.approval_audit()
    assert audit.job_id is None
    assert audit.tool_call_id == "tool1"


async def test_per_channel_events_filter_options_and_cannot_mutate_audit(harness):
    h = harness
    h.channels["telegram"] = HumanChannel("telegram")
    h.policy.settings.approver_channels.append("telegram")
    h.policy.settings.allow_always_approval = True
    with h.bus.subscribe(for_approver("telegram")) as events:
        task, approval = await begin(h)
        event = await asyncio.wait_for(events.get(), 2)
        assert "allow_always" not in [o.kind for o in event.approval.request.options]
        event.approval.request.raw_input["command"] = "tampered"
    h.manager.resolve(approval.id, "reject_once", channel="cli", actor="alice")
    await task
    (audit,) = h.store.approval_audit()
    assert audit.raw_input["command"] == "echo hello"
