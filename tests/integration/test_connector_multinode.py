"""Two real computer connections with the same alias and independent ACP agents."""

import asyncio
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import SecretStr

from acp_gateway.agents import AgentClient, AgentUnavailable, AuthenticationFailed
from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings, TelegramSettings
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.policy import LocalAgentPolicy, PolicyTransport
from acp_gateway.connectors.protocol import AgentManifest, Data, Hello, encode_frame
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.daemon import configured_app
from fakes.fake_goose import FakeGooseServer
from fakes.telegram import FakeTelegramSession


async def eventually(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running_app(app):
    # AnyIO's MCP cancel scope must enter and exit in the same task. Async
    # pytest fixtures may resume their teardown in a different task.
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def run():
        try:
            async with app.router.lifespan_context(app):
                ready.set_result(None)
                await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    task = asyncio.create_task(run())
    try:
        await ready
        yield
    finally:
        stop.set()
        await task


@pytest.fixture
async def nodes(tmp_path, monkeypatch):
    listeners = []
    original = ControlDispatcher.listen

    @contextlib.asynccontextmanager
    async def listen(self, host, port, *, tls=None):
        async with original(self, host, 0, tls=tls) as server:
            listeners.append(server)
            yield server

    monkeypatch.setattr(ControlDispatcher, "listen", listen)
    owner = "mock-multinode-owner-credential"
    config = AppConfig(
        Settings(
            data_dir=tmp_path / "vps",
            connector={"enabled": True},
            agents=[
                dict(
                    alias="goose",
                    kind="goose",
                    backend="connector",
                    computer_id=name,
                    default_cwd="/work",
                )
                for name in ("work", "home", "missing")
            ]
            + [dict(alias="other", backend="connector", computer_id="work", default_cwd="/work")],
        ),
        SecretStore({"ACPGW_API_TOKEN": owner, "ACPGW_MCP_TOKEN": "mock-multinode-mcp-credential"}),
        None,
        None,
    )
    connections, tasks = [], []
    peers, keys, factories, mocks = {}, {}, {}, {}
    with contextlib.ExitStack() as stack:
        for name in ("work", "home"):
            secret = "mock-" + name + "-local-agent-credential"
            goose = stack.enter_context(FakeGooseServer(secret))
            mocks[name] = goose
            profile = AgentProfile(
                alias="goose", kind="goose", url=goose.url("ws"), default_cwd="/work"
            )
            local = AgentClient(profile, SecretStr(secret), pin_dir=tmp_path / name)

            async def factory(alias, local=local, profile=profile):
                assert alias == "goose"
                transport, _ = await local.open_transport()
                return PolicyTransport(transport, LocalAgentPolicy.from_profile(profile))

            factories[name] = factory
        app = stack.enter_context(configured_app(config))
        registry = app.state.core.store.computers
        for name in ("work", "home"):
            keys[name] = tmp_path / (name + ".key")
            registry.issue(name, keys[name], display_name=name.title())
        for client in app.state.core.agents.values():
            client._reconnect_delays = ()
        async with running_app(app):
            url = f"ws://127.0.0.1:{listeners[0].sockets[0].getsockname()[1]}/connect"

            async def computer(name):
                connection, welcome = await connect_control(
                    url,
                    credential=keys[name].read_text().strip(),
                    hello=Hello(
                        computer_id=name,
                        agents=[AgentManifest(alias="goose", display_name="Goose")],
                    ),
                )
                peer = RelayPeer(
                    connection, welcome.connection_id, ["goose"], local_factory=factories[name]
                )
                connections.append(connection)
                tasks.append(asyncio.create_task(peer.run()))
                peers[name] = peer
                await peer.ready.wait()
                await eventually(lambda: app.state.dispatcher.active[name].relay.ready.is_set())
                return peer

            for name in ("work", "home"):
                await computer(name)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://127.0.0.1",
                headers={"Authorization": f"Bearer {owner}"},
            ) as api:
                try:
                    yield SimpleNamespace(
                        app=app,
                        api=api,
                        registry=registry,
                        dispatcher=app.state.dispatcher,
                        core=app.state.core,
                        mocks=mocks,
                        peers=peers,
                        keys=keys,
                        computer=computer,
                    )
                finally:
                    for connection in connections:
                        await connection.close()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)


async def routes(nodes):
    response = await nodes.api.get("/computers")
    assert response.status_code == 200, response.text
    return {r["address"]: r for r in response.json()["routes"]}


async def message(nodes, name, text):
    response = await nodes.api.post(
        "/messages", json={"agent": name + "/goose", "thread": "same", "text": text, "wait": 2}
    )
    assert response.status_code == 202, response.text
    return response.json()


async def test_same_alias_routes_keep_history_sessions_and_mcp_tools_separate(nodes):
    initial = await routes(nodes)
    assert initial["missing/goose"]["problem"] == "not_enrolled"
    assert initial["work/goose"]["problem"] == "agent_not_initialized"
    assert initial["work/other"]["problem"] == "agent_unadvertised"
    assert not nodes.mocks["work"].sessions and not nodes.mocks["home"].sessions
    for name in ("work", "home"):
        assert (await message(nodes, name, "code word " + name))["answer"] == "OK"
    replies = await asyncio.gather(
        message(nodes, "work", "What was the code word"),
        message(nodes, "home", "What was the code word"),
    )
    assert [r["answer"] for r in replies] == ["work", "home"]
    work_sessions = (
        await nodes.api.get("/sessions", params={"agent": "work/goose", "thread": "same"})
    ).json()
    home_sessions = (
        await nodes.api.get("/sessions", params={"agent": "home/goose", "thread": "same"})
    ).json()
    assert work_sessions != home_sessions
    # A local session ID may not be used through the other computer's route.
    work_id = work_sessions["sessions"][0]["id"]
    response = await nodes.api.post(
        f"/sessions/{work_id}/activate", json={"agent": "home/goose", "thread": "same"}
    )
    assert response.status_code == 404
    mcp = nodes.core.channels["mcp"].server
    tools = {t.name for t in await mcp.list_tools()}
    assert {"work__goose_ask", "home__goose_ask"} <= tools
    assert (await mcp.call_tool("work__goose_ask", {"text": "say pong", "wait": 2}))[1][
        "answer"
    ] == "pong"
    assert (await mcp.call_tool("home__goose_ask", {"text": "say pong", "wait": 2}))[1][
        "answer"
    ] == "pong"
    current = await routes(nodes)
    assert current["work/goose"]["epoch"] != current["home/goose"]["epoch"]
    for name in ("work", "home"):
        assert current[name + "/goose"]["agent_ready"]
        assert current[name + "/goose"]["active_streams"] == 1
        assert current[name + "/goose"]["problem"] is None
    await nodes.core.agent("work/goose").close()
    current = await routes(nodes)
    assert current["work/goose"]["active_streams"] == 0
    assert current["work/goose"]["last_stream_error"] is None
    assert current["home/goose"]["agent_ready"]


@pytest.mark.parametrize(
    "failure", ["disconnect", "revoke", "rotate", "replacement", "wrong_epoch", "wrong_alias"]
)
async def test_one_computer_failure_preserves_other_pending_approval(nodes, failure, tmp_path):
    lease = nodes.app.state.cli.attach("multinode-human")
    try:
        jobs = {}
        for name in ("work", "home"):
            response = await nodes.api.post(
                "/messages",
                json={"agent": name + "/goose", "text": "run exactly: echo " + name + " ."},
            )
            jobs[name] = response.json()
        await eventually(lambda: len(nodes.core.pending_approvals("cli")) == 2)
        approvals = (await nodes.api.get("/approvals")).json()["approvals"]
        home = next(a for a in approvals if a["job_id"] == jobs["home"]["id"])
        work = next(a for a in approvals if a["job_id"] == jobs["work"]["id"])
        old_work_epoch = nodes.peers["work"].epoch
        home_epoch = nodes.peers["home"].epoch
        if failure == "disconnect":
            await nodes.peers["work"].connection.close()
        elif failure == "revoke":
            nodes.registry.revoke("work")
        elif failure == "rotate":
            key = tmp_path / "work-next.key"
            nodes.registry.issue("work", key)
            nodes.keys["work"] = key
        elif failure == "replacement":
            await nodes.computer("work")
        elif failure == "wrong_epoch":
            # Even another valid computer's epoch is invalid on this connection.
            await nodes.peers["work"].connection.send(
                encode_frame(
                    Data(
                        epoch=home_epoch,
                        stream=uuid4(),
                        alias="goose",
                        message={"jsonrpc": "2.0", "method": "notice"},
                    )
                )
            )
        else:
            transport = nodes.core.agent("work/goose")._transport
            await nodes.peers["work"].connection.send(
                encode_frame(
                    Data(
                        epoch=old_work_epoch,
                        stream=transport.stream,
                        alias="other",
                        message={"jsonrpc": "2.0", "method": "notice"},
                    )
                )
            )
        failed = await nodes.api.get(f"/jobs/{jobs['work']['id']}", params={"wait": 2})
        assert failed.json()["status"] == "failed", failed.text
        current = await routes(nodes)
        assert current["home/goose"]["agent_ready"]
        assert current["home/goose"]["epoch"] == str(home_epoch)
        assert current["work/goose"]["problem"] == (
            "access_disabled"
            if failure == "revoke"
            else "agent_not_initialized"
            if failure == "replacement"
            else "computer_offline"
        )
        assert nodes.core.agent("home/goose").connected
        assert nodes.peers["home"].epoch == home_epoch and not nodes.peers["home"].closed.is_set()
        assert (await nodes.api.get(f"/jobs/{jobs['home']['id']}")).json()["status"] == "running"
        assert [a.id for a in nodes.core.pending_approvals("cli")] == [home["id"]]
        for approval, expected in ((work, 404), (home, 200)):
            option = next(o for o in approval["request"]["options"] if o["kind"] == "allow_once")
            response = await nodes.api.post(
                f"/approvals/{approval['id']}",
                json={"option_id": option["option_id"], "lease_id": lease},
            )
            assert response.status_code == expected, response.text
        completed = (await nodes.api.get(f"/jobs/{jobs['home']['id']}", params={"wait": 2})).json()
        assert completed["answer"] == "home" and completed["status"] == "completed"
        assert (await message(nodes, "home", "code word healthy"))["answer"] == "OK"
        assert (await message(nodes, "home", "What was the code word"))["answer"] == "healthy"
        if failure == "replacement":
            assert nodes.peers["work"].epoch != old_work_epoch
            assert (await message(nodes, "work", "say pong"))["answer"] == "pong"
        history = [
            item for session in nodes.mocks["work"].sessions.values() for item in session.history
        ]
        assert history.count(("user", "run exactly: echo work .")) == 1
    finally:
        nodes.app.state.cli.detach(lease)


async def test_computer_capacity_and_queue_overflow_are_isolated(nodes, monkeypatch):
    monkeypatch.setattr("acp_gateway.connectors.relay.MAX_STREAMS", 2)
    for name in ("work", "home"):
        assert (await message(nodes, name, "say pong"))["answer"] == "pong"
    stalled = await nodes.dispatcher.open_agent("work", "goose")
    try:
        with pytest.raises(AgentUnavailable, match="stream limit"):
            await nodes.dispatcher.open_agent("work", "goose")
        current = await routes(nodes)
        assert not current["work/goose"]["capacity_available"]
        assert current["work/goose"]["last_stream_error"]["code"] == "stream_limit"
        assert current["home/goose"]["capacity_available"]
        for _ in range(3):
            await nodes.peers["work"].send(
                Data(
                    **stalled.route,
                    message={
                        "jsonrpc": "2.0",
                        "method": "notice",
                        "params": {"text": "x" * 800_000},
                    },
                )
            )
        await eventually(stalled.closed.is_set)
        current = await routes(nodes)
        assert current["work/goose"]["last_stream_error"]["code"] == "overflow"
        assert current["work/goose"]["last_stream_error"]["at"]
        assert current["home/goose"]["last_stream_error"] is None
        for name in ("work", "home"):
            assert (await message(nodes, name, "say pong"))["answer"] == "pong"
    finally:
        await stalled.close()


async def test_local_access_error_is_diagnosed_only_on_its_route_and_clears_on_open(nodes):
    peer = nodes.peers["work"]
    original = peer.local_factory

    async def rejected(alias):
        raise AuthenticationFailed("local credentials rejected")

    peer.local_factory = rejected
    reply = await nodes.api.post("/messages", json={"agent": "work/goose", "text": "say pong"})
    assert reply.status_code == 403
    current = await routes(nodes)
    assert current["work/goose"]["problem"] == "access_denied"
    assert current["work/goose"]["last_stream_error"]["code"] == "access_denied"
    assert current["home/goose"]["last_stream_error"] is None
    assert (await message(nodes, "home", "say pong"))["answer"] == "pong"
    peer.local_factory = original
    assert (await message(nodes, "work", "say pong"))["answer"] == "pong"
    assert (await routes(nodes))["work/goose"]["last_stream_error"] is None


async def test_telegram_selects_computers_and_decides_relay_approvals(nodes):
    owner = 12345
    token = "123456:" + "mock-multinode-telegram-credential"
    session = FakeTelegramSession()
    channel = TelegramChannel(
        TelegramSettings(enabled=True, allowed_user_ids=[owner]),
        SecretStr(token),
        bot=Bot(token, session=session),
    )
    nodes.core.policy.settings.approver_channels = ["telegram"]
    nodes.core.add_channel(channel)
    await channel.start(nodes.core)
    await eventually(lambda: channel._ready)
    user = User(id=owner, is_bot=False, first_name="Owner")

    async def send(number, text):
        await channel.dispatcher.feed_update(
            channel.bot,
            Update(
                update_id=number,
                message=Message(
                    message_id=number,
                    date=datetime.now(UTC),
                    chat=Chat(id=owner, type="private"),
                    from_user=user,
                    text=text,
                ),
            ),
        )

    await send(1, "/agent")
    assert "work/goose" in session.sent[-1].text and "home/goose" in session.sent[-1].text
    for number, name, option, answer in ((2, "home", 0, "home"), (5, "work", 1, "DENIED")):
        # Each segment represents a user interaction, outside the command throttle window.
        channel._recent.clear()
        await send(number, "/agent " + name + "/goose")
        await send(number + 1, "run exactly: echo " + name + " .")
        await eventually(lambda: bool(channel._buttons))
        message = next(
            m
            for m in reversed(session.sent)
            if m.reply_markup and f"Approval: {name}/goose" in m.text
        )
        await channel.dispatcher.feed_update(
            channel.bot,
            Update(
                update_id=number + 2,
                callback_query=CallbackQuery(
                    id=f"decision-{name}",
                    from_user=user,
                    chat_instance="private-test",
                    message=message,
                    data=message.reply_markup.inline_keyboard[0][option].callback_data,
                ),
            ),
        )
        await eventually(lambda answer=answer: any(m.text == answer for m in session.sent))
        await eventually(lambda: not channel._buttons)
    assert len(nodes.mocks["home"].sessions) == len(nodes.mocks["work"].sessions) == 1
