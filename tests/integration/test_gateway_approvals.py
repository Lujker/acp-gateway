"""Full ACP permission RPCs routed through core to a simulated human channel."""

import asyncio

import pytest
from pydantic import SecretStr

from acp_gateway.agents import AgentClient, SessionBusy
from acp_gateway.channels import Channel
from acp_gateway.config import AgentProfile
from acp_gateway.core import ApprovalRequested, GatewayCore, PolicyDenied, for_approver
from acp_gateway.storage import Conversation, JobStatus, Store
from fakes.fake_goose import FakeGooseServer

SECRET = "mock-goose-" + "secret-7781"
MCP = Conversation("mcp", "thread1", "work")


class Human(Channel):
    name = "cli"
    can_approve = True

    async def start(self, core):
        pass

    async def stop(self):
        pass


@pytest.fixture
async def core(tmp_path):
    with FakeGooseServer(SECRET) as server:
        profile = AgentProfile(
            alias="work", kind="goose", url=server.url("ws"), default_cwd="/work"
        )

        # Core must replace even an unsafe handler supplied by the client caller.
        async def unsafe_handler(request):
            return request.option("allow_once").option_id

        client = AgentClient(
            profile,
            SecretStr(SECRET),
            pin_dir=tmp_path / "pins",
            permission_handler=unsafe_handler,
        )
        gateway = GatewayCore(Store.open(tmp_path / "gateway.db"), {"work": client})
        yield gateway, server
        await gateway.close()


async def pending(core):
    core.add_channel(Human())
    with core.bus.subscribe(for_approver("cli")) as events:
        job = await core.submit(MCP, "run exactly: echo hi .")
        event = await asyncio.wait_for(events.get(), 5)
    assert isinstance(event, ApprovalRequested)
    return job, event.approval


@pytest.mark.parametrize("option,answer", [("allow_once", "hi"), ("reject_once", "DENIED")])
async def test_mcp_task_is_decided_by_human_cli(core, option, answer):
    gateway, _ = core
    job, approval = await pending(gateway)
    assert (await gateway.wait(job.id, 0.01)).status is JobStatus.RUNNING
    assert approval.conversation == MCP
    assert approval.request.raw_input["command"] == "echo hi"
    with pytest.raises(PolicyDenied):
        gateway.pending_approvals("mcp")
    gateway.resolve_approval(approval.id, option, channel="cli", actor="owner")
    result = await gateway.wait(job.id, 5)
    assert result.status is JobStatus.COMPLETED
    assert result.answer == answer
    (audit,) = gateway.store.approval_audit(job.id)
    assert audit.actor == "owner"
    assert audit.decided_channel == "cli"


async def test_missing_human_rejects_and_explains_to_originating_channel(core):
    gateway, _ = core
    result = await gateway.ask(MCP, "run exactly: echo hi .", wait=5)
    assert result.answer == "DENIED"
    assert result.error == "no human approver is connected"
    (audit,) = gateway.store.approval_audit(result.id)
    assert audit.outcome == "unavailable"


async def test_approval_timeout_rejects_on_agent(core):
    gateway, _ = core
    gateway.policy.settings.approval_timeout_seconds = 1
    job, _ = await pending(gateway)
    result = await gateway.wait(job.id, 5)
    assert result.answer == "DENIED"
    assert result.error == "approval timed out"
    (audit,) = gateway.store.approval_audit(job.id)
    assert audit.outcome == "timed_out"


async def test_cancel_pending_approval_settles_rpc_and_job(core):
    gateway, _ = core
    job, _ = await pending(gateway)
    result = await gateway.cancel_job(job.id)
    assert result.status is JobStatus.CANCELLED
    assert gateway.pending_approvals("cli") == []
    (audit,) = gateway.store.approval_audit(job.id)
    assert audit.outcome == "cancelled"


async def test_handler_cannot_be_replaced_while_permission_is_pending(core):
    gateway, _ = core
    job, _ = await pending(gateway)
    with pytest.raises(SessionBusy):
        gateway.agent("work").set_permission_handler(lambda request: None)
    await gateway.cancel_job(job.id)


async def test_prompt_limit_rejects_before_creating_agent_session(core):
    gateway, server = core
    gateway.policy.settings.max_prompt_length = 3
    with pytest.raises(PolicyDenied, match="prompt exceeds"):
        await gateway.submit(MCP, "abcd")
    assert server.log == []
    assert gateway.sessions(MCP) == []


async def test_disabled_new_sessions_blocks_implicit_and_explicit_creation(core):
    gateway, server = core
    gateway.policy.settings.allow_new_sessions = False
    with pytest.raises(PolicyDenied):
        await gateway.new_session(MCP)
    with pytest.raises(PolicyDenied):
        await gateway.submit(MCP, "hello")
    assert server.log == []


async def test_existing_session_remains_usable_when_creation_disabled(core):
    gateway, _ = core
    await gateway.new_session(MCP)
    gateway.policy.settings.allow_new_sessions = False
    assert (await gateway.ask(MCP, "say pong", wait=5)).answer == "pong"


async def test_response_limit_caps_stored_answer_and_fails_job(core):
    gateway, _ = core
    gateway.policy.settings.max_response_length = 3
    result = await gateway.ask(MCP, "hello", wait=5)
    assert result.status is JobStatus.FAILED
    assert result.answer == "ech"
    assert result.error == "response exceeds 3 characters"
    assert gateway.store.job(result.id).answer == "ech"
    # The following turn uses the released session successfully.
    gateway.policy.settings.max_response_length = 4
    assert (await gateway.ask(MCP, "say pong", wait=5)).answer == "pong"


async def test_cancel_policy_does_not_prevent_shutdown(core, tmp_path):
    gateway, _ = core
    job, _ = await pending(gateway)
    gateway.policy.settings.allow_cancel = False
    with pytest.raises(PolicyDenied):
        await gateway.cancel_job(job.id)
    with pytest.raises(PolicyDenied):
        await gateway.cancel(MCP)
    await gateway.close()

    reopened = Store.open(tmp_path / "gateway.db")
    try:
        (audit,) = reopened.approval_audit(job.id)
        assert audit.outcome == "cancelled"
        assert reopened.job(job.id).status is JobStatus.INTERRUPTED
    finally:
        reopened.close()
