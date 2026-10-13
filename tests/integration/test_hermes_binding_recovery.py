import asyncio

import pytest
from test_hermes_acp import hermes as hermes

from acp_gateway.agents import AgentClient, SessionNotFound
from acp_gateway.core import GatewayCore
from acp_gateway.storage import Conversation, Store


async def test_hermes_provenance_survives_gateway_restart_without_adoption(hermes, tmp_path):
    backend, client = hermes
    path = tmp_path / "gateway.db"
    conversation = Conversation("telegram", "123", "hermes")
    core = GatewayCore(Store.open(path), {"hermes": client})
    await core.start()
    session = await core.new_session(conversation)
    assert session.binding == {
        "backend": "hermes",
        "mode": "dedicated",
        "profile": "default",
        "runtime_id": "live-1",
        "stored_id": "stored-live-1",
        "restore": "disabled",
    }
    assert session.availability == "attached"
    job = await core.ask(conversation, "kept answer")
    await core.close()
    before = list(backend.calls)

    replacement = AgentClient(client.profile, pin_dir=tmp_path / "pins2", reconnect_delays=())
    core = GatewayCore(Store.open(path), {"hermes": replacement})
    await core.start()
    try:
        listed = core.sessions(conversation)
        assert len(listed) == 1 and listed[0].binding == session.binding
        assert listed[0].availability == "restore-unavailable"
        assert core.active_session(conversation).acp_session_id == session.acp_session_id
        assert core.job(job.id).answer == "kept answer"
        with pytest.raises(SessionNotFound, match="/new"):
            await core.submit(conversation, "do not replay")
        assert backend.calls == before  # No reconnect/resume/prompt or implicit creation.
        fresh = await core.new_session(conversation)
        assert fresh.acp_session_id != session.acp_session_id
        assert fresh.binding["stored_id"] != session.binding["stored_id"]
        assert (
            await core.ask(conversation, "explicit new dialogue")
        ).answer == "explicit new dialogue"
        assert len(core.sessions(conversation)) == 2
    finally:
        await core.close()


async def test_hermes_session_busy_and_unknown_outcome_are_visible_without_replay(hermes, tmp_path):
    backend, client = hermes
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"hermes": client})
    await core.start()
    conversation = Conversation("cli", "default", "hermes")
    try:
        job = await core.submit(conversation, "hold")
        await asyncio.wait_for(backend.started.wait(), 2)
        assert core.active_session(conversation).availability == "busy"
        backend.release.set()
        await core.wait(job.id, timeout=2)
        assert core.active_session(conversation).availability == "attached"
        failed = await core.ask(conversation, "interfere")
        assert failed.error and core.active_session(conversation).availability == "uncertain"
        before = list(backend.calls)
        with pytest.raises(SessionNotFound, match="/new"):
            await core.submit(conversation, "try again")
        assert backend.calls == before
    finally:
        await core.close()
