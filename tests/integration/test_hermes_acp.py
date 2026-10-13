"""Real host HTTP/WS -> dedicated Hermes driver -> ACP SDK (no model/tools)."""

import asyncio

import pytest

from acp_gateway.agents import (
    AgentClient,
    AgentError,
    PromptFailed,
    SessionNotFound,
    TransportDisconnected,
)
from acp_gateway.channels.base import Channel
from acp_gateway.config import AgentProfile
from acp_gateway.core import GatewayCore
from acp_gateway.storage import Conversation, Store
from fakes.fake_hermes import FakeHermes


@pytest.fixture
async def hermes(tmp_path):
    async with FakeHermes(tmp_path / "locks") as backend:
        profile = AgentProfile(
            alias="hermes",
            kind="hermes",
            backend="hermes",
            default_cwd=r"C:\work",
            hermes={"lock_dir": backend.directory},
        )
        client = AgentClient(
            profile, pin_dir=tmp_path / "pins", reconnect_delays=(), open_timeout=1
        )
        try:
            yield backend, client
        finally:
            await client.close()


async def test_sdk_roundtrip_streaming_final_fallback_and_isolation(hermes):
    backend, client = hermes
    session = await client.new_session()
    assert not client.agent_capabilities.load_session
    assert not client.supports_cancel
    assert backend.sessions["live-1"]["cwd"] == r"C:\work"
    assert (await client.ask(session, "chunks")).text == "hello world"
    assert (await client.ask(session, "final only")).text == "final only"
    assert [s["sessionId"] for s in await client.list_sessions()] == [session]
    assert backend.calls[0]["params"] == {"server_requests": True}


@pytest.mark.parametrize("approve", [True, False, None])
async def test_sdk_permissions_only_once_no_foreign_decisions(hermes, approve):
    backend, client = hermes
    decisions = []

    async def decide(request):
        decisions.append(request)
        assert {o.kind for o in request.options} == {"allow_once", "reject_once"}
        return request.option("allow_once" if approve else "reject_once").option_id

    if approve is not None:
        client.set_permission_handler(decide)
    session = await client.new_session()
    reply = await asyncio.wait_for(client.ask(session, "permit"), 2)
    assert reply.text == ("allowed" if approve else "denied")
    assert len(decisions) == (0 if approve is None else 1)
    assert len(backend.replies) == 1
    assert backend.replies[0]["id"] != "foreign"
    assert backend.replies[0]["result"]["choice"] == ("once" if approve else "deny")


async def test_sdk_withdrawn_permission_cannot_be_approved_late(hermes):
    backend, client = hermes
    selected = asyncio.Event()

    async def decide(request):
        await selected.wait()
        return request.option("allow_once").option_id

    client.set_permission_handler(decide)
    session = await client.new_session()
    turn = asyncio.create_task(client.ask(session, "withdraw"))
    await asyncio.wait_for(backend.withdrawn.wait(), 2)
    # Let the cancel event traverse both driver and bridge before the late reply.
    async with asyncio.timeout(2):
        while client._transport._permissions:
            await asyncio.sleep(0.001)
    selected.set()
    await asyncio.sleep(0.05)
    assert not backend.replies
    backend.release.set()
    assert (await asyncio.wait_for(turn, 2)).text == "withdrawn"


async def test_sdk_no_adoption_resume_or_fake_cancellation(hermes):
    backend, client = hermes
    session = await client.new_session()
    with pytest.raises(SessionNotFound):
        await client.load_session("desktop-private")
    with pytest.raises(AgentError, match="cancellation is unavailable"):
        await client.cancel(session)
    assert [f["method"] for f in backend.calls] == ["client.capabilities", "session.create"]


async def test_nonisolated_submit_fails_and_never_retries_or_interrupts(hermes):
    backend, client = hermes
    session = await client.new_session()
    with pytest.raises(PromptFailed):
        await client.ask(session, "interfere")
    before = list(backend.calls)
    with pytest.raises(SessionNotFound):
        await client.ask(session, "retry")
    assert backend.calls == before
    assert len([f for f in backend.calls if f["method"] == "prompt.submit"]) == 1


async def test_sdk_drop_fails_pending_turn_and_cannot_restore_old_handle(hermes):
    backend, client = hermes
    session = await client.new_session()
    turn = asyncio.create_task(client.ask(session, "hold"))
    await asyncio.wait_for(backend.started.wait(), 2)
    await backend.sockets[0].close()
    with pytest.raises(TransportDisconnected):
        await asyncio.wait_for(turn, 2)
    with pytest.raises(SessionNotFound):
        await client.ask(session, "retry")
    assert len([f for f in backend.calls if f["method"] == "prompt.submit"]) == 1


async def test_core_cancel_refusal_preserves_running_job(hermes, tmp_path):
    backend, client = hermes
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"hermes": client})
    await core.start()
    try:
        job = await core.submit(Conversation("cli", "test", "hermes"), "hold")
        await asyncio.wait_for(backend.started.wait(), 2)
        with pytest.raises(AgentError, match="cancellation is unavailable"):
            await core.cancel_job(job.id)
        assert not core._running[job.id].cancel_requested
        assert len(core.running_jobs()) == 1
        backend.release.set()
        final = await core.wait(job.id, timeout=2)
        assert final.answer == "released"
    finally:
        await core.close()


class HumanChannel(Channel):
    name = "cli"
    can_approve = True

    async def start(self, core):
        pass

    async def stop(self):
        pass


async def test_upstream_withdrawal_clears_core_human_approval_card(hermes, tmp_path):
    backend, client = hermes
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"hermes": client})
    core.add_channel(HumanChannel())
    await core.start()
    try:
        job = await core.submit(Conversation("cli", "test", "hermes"), "withdraw")
        async with asyncio.timeout(2):
            while not core.pending_approvals("cli"):
                await asyncio.sleep(0.001)
        await asyncio.wait_for(backend.withdrawn.wait(), 2)
        async with asyncio.timeout(2):
            while core.pending_approvals("cli"):
                await asyncio.sleep(0.001)
        assert not backend.replies
        backend.release.set()
        assert (await core.wait(job.id, timeout=2)).answer == "withdrawn"
    finally:
        await core.close()
