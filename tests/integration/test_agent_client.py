"""AgentClient against the recorded-traffic goose mock (P1.1 acceptance)."""

import asyncio
import io
import socket

import pytest
from pydantic import SecretStr

from acp_gateway.agents import (
    AgentClient,
    AgentUnavailable,
    AuthenticationFailed,
    ModeNotAvailable,
    PermissionRequest,
    SessionBusy,
    SessionNotFound,
    TLSFingerprintMismatch,
    TransportDisconnected,
)
from acp_gateway.agents.events import (
    MessageChunk,
    ModeChanged,
    SessionInfoUpdated,
    ToolCallStarted,
    ToolCallUpdated,
    TurnFinished,
    UsageUpdated,
)
from acp_gateway.config import AgentProfile
from acp_gateway.log import configure_logging
from fakes.certs import fingerprint, make_cert
from fakes.fake_goose import FakeGooseServer, free_port

SECRET = "mock-goose-" + "secret-7781"


@pytest.fixture
def server():
    with FakeGooseServer(SECRET) as srv:
        yield srv


def profile(url: str, **overrides) -> AgentProfile:
    fields = {
        "alias": "work",
        "title": "Work Goose",
        "kind": "goose",
        "url": url,
        "secret_env": "AGENT_WORK_SECRET",
        "default_cwd": "/home/user/work",
    }
    return AgentProfile(**(fields | overrides))


def client_for(url: str, tmp_path, *, secret: str = SECRET, handler=None, **overrides):
    kwargs = {"pin_dir": tmp_path / "pins", "reconnect_delays": (0.05, 0.1), "open_timeout": 5}
    if handler is not None:
        kwargs["permission_handler"] = handler
    return AgentClient(profile(url, **overrides), SecretStr(secret), **kwargs)


def allow(request: PermissionRequest):
    async def _allow(req: PermissionRequest) -> str | None:
        return req.option("allow_once").option_id

    return _allow(request)


async def collect(client: AgentClient, session_id: str, text: str) -> list:
    return [event async for event in client.prompt(session_id, text)]


# --------------------------------------------------------------- connection


async def test_connect_advertises_no_client_capabilities(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path)
    await client.connect()
    try:
        caps = server.agents[0].client_capabilities
        assert caps.fs.read_text_file is False
        assert caps.fs.write_text_file is False
        assert caps.terminal is False
        assert client.agent_info["name"] == "goose"
        assert client.agent_capabilities.load_session is True
    finally:
        await client.close()


async def test_base_url_without_acp_path(server, tmp_path):
    client = client_for(server.url("ws").removesuffix("/acp"), tmp_path)
    try:
        session_id = await client.new_session()
        assert (await client.ask(session_id, "say pong")).text == "pong"
    finally:
        await client.close()


async def test_new_session_applies_smart_approve(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path)
    try:
        session_id = await client.new_session()
        assert server.sessions[session_id].mode == "smart_approve"
        assert "new_session mcp_servers=0" in server.log
        assert "set_mode smart_approve" in server.log
    finally:
        await client.close()


async def test_unknown_mode_is_refused(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path, session_mode="turbo")
    try:
        with pytest.raises(ModeNotAvailable, match="turbo"):
            await client.new_session()
    finally:
        await client.close()


async def test_wrong_secret_fails_fast(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path, secret="wrong-" + "secret-value")
    with pytest.raises(AuthenticationFailed):
        await client.ensure_connected()
    assert server.log == []


async def test_unreachable_agent_after_retries(tmp_path):
    client = client_for(f"ws://127.0.0.1:{free_port()}/acp", tmp_path)
    with pytest.raises(AgentUnavailable, match="Work Goose is unavailable"):
        await client.ensure_connected()


# -------------------------------------------------------------------- turns


async def test_turn_streams_normalized_events(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path)
    try:
        session_id = await client.new_session()
        events = await collect(client, session_id, "Reply with exactly one word: pong")
    finally:
        await client.close()

    assert isinstance(events[-1], TurnFinished)
    assert events[-1].stop_reason == "end_turn"
    assert events[-1].usage["totalTokens"] == 25140
    kinds = [type(e) for e in events]
    assert SessionInfoUpdated in kinds
    assert UsageUpdated in kinds
    assert ModeChanged not in kinds  # set_mode happened before the turn
    assert "".join(e.text for e in events if isinstance(e, MessageChunk)) == "pong"


async def test_twenty_sequential_prompts(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path)
    try:
        session_id = await client.new_session()
        for i in range(20):
            reply = await client.ask(session_id, f"message {i}")
            assert reply.text == f"echo: message {i}"
            assert reply.stop_reason == "end_turn"
    finally:
        await client.close()


async def test_permission_rejected_by_default(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path)
    try:
        session_id = await client.new_session()
        events = await collect(client, session_id, "run exactly: echo hi .")
    finally:
        await client.close()
    assert "".join(e.text for e in events if isinstance(e, MessageChunk)) == "DENIED"
    updates = [e for e in events if isinstance(e, ToolCallUpdated)]
    assert updates[-1].status == "failed"


async def test_permission_request_details_and_allow(server, tmp_path):
    seen: list[PermissionRequest] = []

    async def handler(request: PermissionRequest) -> str | None:
        seen.append(request)
        return request.option("allow_once").option_id

    client = client_for(server.url("ws"), tmp_path, handler=handler)
    try:
        session_id = await client.new_session()
        reply = await client.ask(session_id, "run exactly: echo hello-acp .")
    finally:
        await client.close()

    assert reply.text == "hello-acp"
    (request,) = seen
    assert request.title == "shell · echo hello-acp"
    assert request.raw_input == {"command": "echo hello-acp", "timeout_secs": 30}
    assert [o.kind for o in request.options] == [
        "allow_always",
        "allow_once",
        "reject_once",
        "reject_always",
    ]


async def test_unknown_option_from_handler_is_rejected(server, tmp_path):
    async def handler(request: PermissionRequest) -> str | None:
        return "yolo"

    client = client_for(server.url("ws"), tmp_path, handler=handler)
    try:
        session_id = await client.new_session()
        assert (await client.ask(session_id, "run exactly: echo x .")).text == "DENIED"
    finally:
        await client.close()


async def test_cancel_while_permission_pending(server, tmp_path):
    asked = asyncio.Event()

    async def never_answer(request: PermissionRequest) -> str | None:
        asked.set()
        await asyncio.Event().wait()
        return None

    client = client_for(server.url("ws"), tmp_path, handler=never_answer)
    try:
        session_id = await client.new_session()
        turn = asyncio.create_task(collect(client, session_id, "run exactly: sleep 30 ."))
        await asyncio.wait_for(asked.wait(), timeout=10)
        await client.cancel(session_id)
        events = await asyncio.wait_for(turn, timeout=5)
    finally:
        await client.close()
    assert events[-1].stop_reason == "cancelled"


async def test_cancel_while_command_runs(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path, handler=allow)
    try:
        session_id = await client.new_session()
        turn = asyncio.create_task(collect(client, session_id, "run exactly: sleep 30 ."))
        await asyncio.sleep(0.5)
        await client.cancel(session_id)
        events = await asyncio.wait_for(turn, timeout=5)
    finally:
        await client.close()
    assert events[-1].stop_reason == "cancelled"
    assert any(isinstance(e, ToolCallStarted) for e in events)


async def test_closing_the_stream_cancels_the_turn(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path, handler=allow)
    try:
        session_id = await client.new_session()
        stream = client.prompt(session_id, "run exactly: sleep 30 .")
        async for event in stream:
            if isinstance(event, ToolCallStarted):
                break
        await stream.aclose()
        assert "cancel" in server.log
        assert (await client.ask(session_id, "say pong")).text == "pong"  # session is free again
    finally:
        await client.close()


async def test_second_prompt_in_busy_session(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path, handler=allow)
    try:
        session_id = await client.new_session()
        turn = asyncio.create_task(collect(client, session_id, "run exactly: sleep 30 ."))
        await asyncio.sleep(0.3)
        with pytest.raises(SessionBusy):
            await client.ask(session_id, "say pong")
        await client.cancel(session_id)
        await asyncio.wait_for(turn, timeout=5)
    finally:
        await client.close()


# ----------------------------------------------------------------- sessions


async def test_session_is_loaded_on_a_new_connection_without_replay(server, tmp_path):
    first = client_for(server.url("ws"), tmp_path)
    try:
        session_id = await first.new_session()
        await first.ask(session_id, "Remember the code word lynx1234. Reply: OK")
    finally:
        await first.close()

    second = client_for(server.url("ws"), tmp_path)
    try:
        events = await collect(second, session_id, "What was the code word?")
    finally:
        await second.close()
    texts = [e.text for e in events if isinstance(e, MessageChunk)]
    assert texts == ["lynx1234"]  # history replay ("OK", the old prompt) suppressed
    assert "load_session" in server.log


async def test_unknown_session(server, tmp_path):
    client = client_for(server.url("ws"), tmp_path)
    try:
        with pytest.raises(SessionNotFound):
            await client.ask("19990101_1", "hello")
    finally:
        await client.close()


async def test_reconnects_after_agent_restart(tmp_path):
    first = FakeGooseServer(SECRET).start()
    client = client_for(first.url("ws"), tmp_path)
    try:
        session_id = await client.new_session()
        await client.ask(session_id, "Remember the code word fox42. Reply: OK")
        first.stop()
        await asyncio.sleep(0.3)
        assert not client.connected

        second = FakeGooseServer(SECRET, port=first.port, sessions=first.sessions).start()
        try:
            reply = await client.ask(session_id, "What was the code word?")
        finally:
            second.stop()
    finally:
        await client.close()
    assert reply.text == "fox42"
    assert "load_session" in second.log


async def test_disconnect_during_turn(tmp_path):
    srv = FakeGooseServer(SECRET).start()
    client = client_for(srv.url("ws"), tmp_path, handler=allow)
    try:
        session_id = await client.new_session()
        stream = client.prompt(session_id, "run exactly: sleep 30 .")
        async for event in stream:
            if isinstance(event, ToolCallStarted):
                break
        await asyncio.to_thread(srv.stop)
        with pytest.raises(TransportDisconnected):
            async for _ in stream:
                pass
    finally:
        await client.close()
        srv.stop()


# ---------------------------------------------------------------------- TLS


async def test_tls_with_configured_pin(tmp_path):
    cert, key = make_cert(tmp_path)
    with FakeGooseServer(SECRET, certfile=cert, keyfile=key) as srv:
        client = client_for(srv.url("wss"), tmp_path, tls_fingerprint=fingerprint(cert))
        try:
            session_id = await client.new_session()
            assert (await client.ask(session_id, "say pong")).text == "pong"
            assert client.tls_pin.source == "config"
        finally:
            await client.close()


async def test_tls_trust_on_first_use_is_saved(tmp_path):
    cert, key = make_cert(tmp_path)
    with FakeGooseServer(SECRET, certfile=cert, keyfile=key) as srv:
        for expected in ("tofu-new", "tofu-saved"):
            client = client_for(srv.url("wss"), tmp_path)
            try:
                await client.connect()
                assert client.tls_pin.source == expected
            finally:
                await client.close()
    assert (tmp_path / "pins" / "work.sha256").read_text().strip() == fingerprint(cert)


async def test_tls_pin_mismatch_never_sends_the_secret(tmp_path):
    cert, key = make_cert(tmp_path)
    with FakeGooseServer(SECRET, certfile=cert, keyfile=key) as srv:
        client = client_for(srv.url("wss"), tmp_path, tls_fingerprint="AB" * 32)
        with pytest.raises(TLSFingerprintMismatch):
            await client.ensure_connected()
        assert srv.log == []


# ------------------------------------------------------------------ logging


async def test_secret_never_logged(server, tmp_path):
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream, extra_secrets=[SECRET])
    client = client_for(server.url("ws"), tmp_path, handler=allow)
    try:
        session_id = await client.new_session()
        await client.ask(session_id, "run exactly: echo done .")
    finally:
        await client.close()
    output = stream.getvalue()
    assert "agent connected" in output
    assert "permission requested" in output
    assert "agent connection closed" not in output  # a deliberate close is not a drop
    assert SECRET not in output


def test_free_port_helper_returns_unused_port():
    port = free_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
