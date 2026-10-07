"""Failure paths must not leave a session usable in an unsafe state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp.schema import SessionModeState

from acp_gateway.agents import AgentClient, AgentError, SessionBusy
from acp_gateway.agents.events import MessageChunk
from acp_gateway.agents.transport import PinnedWebSocketTransport
from acp_gateway.config import AgentProfile


@pytest.fixture
def client(tmp_path):
    profile = AgentProfile(
        alias="work", kind="goose", url="ws://127.0.0.1:3284/acp", default_cwd="/default"
    )
    client = AgentClient(profile, pin_dir=tmp_path / "pins")
    client._transport = SimpleNamespace(closed=asyncio.Event(), close=AsyncMock())
    client.agent_capabilities = SimpleNamespace(load_session=True)
    return client


@pytest.mark.parametrize("operation", ["new", "load"])
async def test_failed_mode_switch_is_retried_before_prompt(client, operation):
    modes = SessionModeState(
        current_mode_id="auto", available_modes=[{"id": "smart_approve", "name": "Smart"}]
    )
    client._conn = SimpleNamespace(
        new_session=AsyncMock(return_value=SimpleNamespace(session_id="s1", modes=modes)),
        load_session=AsyncMock(return_value=SimpleNamespace(modes=modes)),
        set_session_mode=AsyncMock(side_effect=AgentError("mode rejected")),
        prompt=AsyncMock(return_value=SimpleNamespace(stop_reason="end_turn", usage=None)),
    )
    with pytest.raises(AgentError, match="mode rejected"):
        if operation == "new":
            await client.new_session()
        else:
            await client.load_session("s1")
    with pytest.raises(AgentError, match="mode rejected"):
        await client.ask("s1", "run a command")
    client._conn.prompt.assert_not_awaited()

    client._conn.set_session_mode.side_effect = None
    await client.ask("s1", "run a command")
    assert client._conn.set_session_mode.await_count == 3
    client._conn.prompt.assert_awaited_once()


async def test_failed_reload_invalidates_previously_attached_session(client):
    client._attached.add("s1")
    client._conn = SimpleNamespace(
        load_session=AsyncMock(side_effect=AgentError("load rejected")), prompt=AsyncMock()
    )
    with pytest.raises(AgentError, match="load rejected"):
        await client.load_session("s1")
    with pytest.raises(AgentError, match="load rejected"):
        await client.ask("s1", "hello")
    assert client._conn.load_session.await_count == 2
    client._conn.prompt.assert_not_awaited()


async def test_unconfirmed_cancel_finishes_local_request_and_blocks_session(client, monkeypatch):
    monkeypatch.setattr("acp_gateway.agents.client.DEFAULT_CANCEL_TIMEOUT", 0.01)
    request_finished = asyncio.Event()

    async def never_finishes(**kwargs):
        client._turns["s1"].put_nowait(MessageChunk("s1", "partial"))
        try:
            await asyncio.Event().wait()
        finally:
            request_finished.set()

    client._conn = SimpleNamespace(prompt=never_finishes, cancel=AsyncMock(), close=AsyncMock())
    client._attached.add("s1")
    stream = client.prompt("s1", "hello")
    assert (await anext(stream)).text == "partial"
    with pytest.raises(SessionBusy):
        await client.ask("s1", "overlapping request")
    await stream.aclose()
    assert request_finished.is_set()
    with pytest.raises(SessionBusy):
        await client.ask("s1", "next request")
    with pytest.raises(SessionBusy):
        await client.load_session("s1")
    # A late permission request must not invoke the human handler.
    client._permission_handler = AsyncMock()
    response = await client._on_permission("s1", None, [])
    assert response.outcome.outcome == "cancelled"
    client._permission_handler.assert_not_awaited()
    # Other sessions on the connection remain usable.
    client._conn.prompt = AsyncMock(
        return_value=SimpleNamespace(stop_reason="end_turn", usage=None)
    )
    client._attached.add("s2")
    await client.ask("s2", "independent request")
    await client.close()

    # Reconnecting removes the quarantine; the old session must be loaded again.
    transport = PinnedWebSocketTransport(SimpleNamespace(close=AsyncMock()))
    modes = SessionModeState(current_mode_id="smart_approve", available_modes=[])
    conn = SimpleNamespace(
        initialize=AsyncMock(
            return_value=SimpleNamespace(
                agent_capabilities=SimpleNamespace(load_session=True), agent_info=None
            )
        ),
        load_session=AsyncMock(return_value=SimpleNamespace(modes=modes)),
        prompt=AsyncMock(return_value=SimpleNamespace(stop_reason="end_turn", usage=None)),
        close=AsyncMock(side_effect=transport.close),
    )
    monkeypatch.setattr(PinnedWebSocketTransport, "connect", AsyncMock(return_value=transport))
    monkeypatch.setattr("acp_gateway.agents.client.connect_to_agent", lambda *_: conn)
    try:
        await client.ask("s1", "request after reconnect")
        conn.load_session.assert_awaited_once()
        conn.prompt.assert_awaited_once()
    finally:
        await client.close()


async def test_client_restores_custom_cwd_after_reconnect(client):
    modes = SessionModeState(current_mode_id="smart_approve", available_modes=[])
    client._conn = SimpleNamespace(
        new_session=AsyncMock(return_value=SimpleNamespace(session_id="s1", modes=modes)),
        load_session=AsyncMock(return_value=SimpleNamespace(modes=modes)),
        prompt=AsyncMock(return_value=SimpleNamespace(stop_reason="end_turn", usage=None)),
    )
    await client.new_session("/custom")
    client._attached.clear()
    await client.ask("s1", "hello")
    assert client._conn.load_session.call_args.kwargs["cwd"] == "/custom"
