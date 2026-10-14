"""Telegram's real dispatcher/session contracts against the recorded ACP mock."""

import asyncio
import time
from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, GetUpdates, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import SecretStr, ValidationError

from acp_gateway.agents import AgentClient
from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.config import AgentProfile, PolicySettings, TelegramSettings
from acp_gateway.core import GatewayCore
from acp_gateway.storage import Conversation, JobStatus, Store
from fakes.fake_goose import FakeGooseServer
from fakes.telegram import FakeTelegramSession as FakeSession

OWNER = 12345
OTHER = 54321
TOKEN = "123456:" + "test-telegram-credential"
SECRET = "mock-goose-" + "secret-7781"


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def telegram(tmp_path):
    with FakeGooseServer(SECRET) as server:
        profile = AgentProfile(
            alias="work", kind="goose", url=server.url("ws"), default_cwd="/work"
        )
        agent = AgentClient(profile, SecretStr(SECRET), pin_dir=tmp_path / "pins")
        core = GatewayCore(
            Store.open(tmp_path / "gateway.db"),
            {"work": agent},
            policy=PolicySettings(approver_channels=["telegram"]),
        )
        session = FakeSession()
        channel = TelegramChannel(
            TelegramSettings(enabled=True, allowed_user_ids=[OWNER, OTHER]),
            SecretStr(TOKEN),
            bot=Bot(TOKEN, session=session),
        )
        core.add_channel(channel)
        await core.start()
        await eventually(lambda: channel._ready)
        yield core, channel, session, server
        await core.close()
        assert session.closed
        assert not channel.connected
        assert not channel._tasks


async def feed(channel, number, text, *, user=OWNER, chat=None, kind="private", is_bot=False):
    message = Message(
        message_id=number,
        date=datetime.now(UTC),
        chat=Chat(id=user if chat is None else chat, type=kind),
        from_user=User(id=user, is_bot=is_bot, first_name="Owner"),
        text=text,
    )
    await channel.dispatcher.feed_update(channel.bot, Update(update_id=number, message=message))


async def click(channel, number, message, data, user=OWNER):
    query = CallbackQuery(
        id=f"click{number}",
        from_user=User(id=user, is_bot=False, first_name="Owner"),
        chat_instance="private-test",
        message=message,
        data=data,
    )
    await channel.dispatcher.feed_update(
        channel.bot, Update(update_id=number, callback_query=query)
    )


@pytest.mark.parametrize(
    "params", [{"user": 99}, {"kind": "group"}, {"chat": OTHER}, {"is_bot": True}]
)
async def test_unknown_users_groups_mismatched_chats_and_bots_are_ignored(telegram, params):
    core, channel, session, server = telegram
    await feed(channel, 1, "say pong", **params)
    assert not channel.connected
    assert not session.sent and not server.log
    assert core.running_jobs() == []


async def test_commands_durable_cursor_sessions_and_final_answer(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    assert channel.connected
    await feed(channel, 2, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    assert core.active_session(conv)
    await feed(channel, 3, "say pong")
    await eventually(lambda: any(m.text == "pong" for m in session.sent))
    prompt_count = core.store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    # Duplicate Telegram update cannot submit twice.
    await feed(channel, 3, "say pong")
    assert core.store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == prompt_count == 1
    await feed(channel, 4, "/sessions")
    assert "*" in session.sent[-1].text
    assert core.store.channel_state(channel.namespace, "cursor")["id"] == 4
    assert core.store.channel_state(channel.namespace, "chats") == [OWNER]


@pytest.mark.parametrize("index,answer", [(0, "hi"), (1, "DENIED")])
async def test_mcp_approval_bound_to_owner_chat_message_and_request(telegram, index, answer):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    job = await core.submit(Conversation("mcp", "test", "work"), "run exactly: echo hi .")
    await eventually(lambda: bool(channel._buttons))
    button_message = next(m for m in session.sent if m.reply_markup)
    buttons = button_message.reply_markup.inline_keyboard[0]
    assert len(buttons) == 2
    data = buttons[index].callback_data
    # Even another allowlisted owner cannot click this owner's private message.
    await click(channel, 2, button_message, data, user=OTHER)
    assert core.job(job.id).status is JobStatus.RUNNING
    await click(channel, 3, button_message.model_copy(update={"message_id": 999}), data)
    assert core.job(job.id).status is JobStatus.RUNNING
    await click(channel, 4, button_message, data.replace("a:", "a:wrong", 1))
    assert core.job(job.id).status is JobStatus.RUNNING
    await click(channel, 5, button_message, data)
    result = await core.wait(job.id, 5)
    assert result.answer == answer
    (audit,) = core.store.approval_audit(job.id)
    assert audit.actor == f"telegram:{OWNER}" and audit.decided_channel == "telegram"
    await click(channel, 6, button_message, data)
    assert len(core.store.approval_audit(job.id)) == 1
    assert any(isinstance(c, AnswerCallbackQuery) for c in session.calls)


async def test_private_job_session_ownership_and_rate_limit(telegram):
    core, channel, session, _ = telegram
    foreign = core.store.add_session(Conversation("cli", "other", "work"), "foreign", "/work")
    job = core.store.add_job("foreign-job", foreign.id)
    await feed(channel, 1, f"/switch {foreign.id}")
    assert "does not own" in session.sent[-1].text
    await feed(channel, 2, f"/result {job.id}")
    assert "does not own" in session.sent[-1].text
    for number in range(3, 9):
        await feed(channel, number, "/help")
    assert sum("Too many" in m.text for m in session.sent) == 1
    assert core.active_session(Conversation("telegram", str(OWNER), "work")) is None


async def test_html_unicode_splitting_retry_and_ambiguous_network_failure(telegram):
    _, channel, session, _ = telegram
    session.failures.append(lambda m: TelegramRetryAfter(method=m, message="retry", retry_after=0))
    await channel._send(OWNER, "<script>" + "😀" * 5000 + TOKEN)
    assert len(session.sent) == 3
    assert "&lt;script&gt;" in session.sent[0].text
    assert TOKEN not in "".join(m.text for m in session.sent)
    before = len(session.calls)
    session.failures.append(lambda m: TelegramNetworkError(method=m, message="ambiguous timeout"))
    with pytest.raises(TelegramNetworkError):
        await channel._send(OWNER, "Do not replay")
    assert len(session.calls) == before + 1


async def test_random_update_id_after_inactive_week_and_disconnected_approver(telegram):
    core, channel, session, _ = telegram
    core.store.set_channel_state(
        channel.namespace, "cursor", {"id": 9999, "time": time.time() - 7 * 86400}
    )
    await feed(channel, 1, "/start")
    assert session.sent
    channel._ready = False

    # An API failure must immediately remove human approval eligibility.
    async def failed(bot, method):
        raise TelegramNetworkError(method=method, message="offline")

    with pytest.raises(TelegramNetworkError):
        await channel._request(failed, channel.bot, GetUpdates())
    assert not channel.connected


async def test_restart_preserves_chat_session_and_suppresses_replayed_command(telegram, tmp_path):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    await feed(channel, 3, "say pong")
    await eventually(lambda: any(m.text == "pong" for m in session.sent))
    profile = core.agent("work").profile
    await core.close()
    reopened = GatewayCore(
        Store.open(tmp_path / "gateway.db"),
        {"work": AgentClient(profile, SecretStr(SECRET), pin_dir=tmp_path / "pins")},
        policy=PolicySettings(approver_channels=["telegram"]),
    )
    fresh_session = FakeSession()
    fresh_channel = TelegramChannel(
        channel.settings, SecretStr(TOKEN), bot=Bot(TOKEN, session=fresh_session)
    )
    reopened.add_channel(fresh_channel)
    try:
        await reopened.start()
        await eventually(lambda: fresh_channel.connected)
        await feed(fresh_channel, 3, "say pong")
        assert not fresh_session.sent
        await feed(fresh_channel, 4, "say pong")
        await eventually(lambda: any(m.text == "pong" for m in fresh_session.sent))
        conv = Conversation("telegram", str(OWNER), "work")
        assert len(reopened.sessions(conv)) == 1
        assert reopened.store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        await reopened.close()


async def test_expired_button_cannot_allow_and_stop_cancels_pending_turn(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    core.policy.settings.approval_timeout_seconds = 1
    job = await core.submit(Conversation("telegram", str(OWNER), "work"), "run exactly: echo hi .")
    await eventually(lambda: bool(channel._buttons))
    msg = next(m for m in session.sent if m.reply_markup)
    data = msg.reply_markup.inline_keyboard[0][0].callback_data
    result = await core.wait(job.id, 3)
    assert result.answer == "DENIED"
    await click(channel, 2, msg, data)
    assert core.store.approval_audit(job.id)[0].outcome == "timed_out"
    core.policy.settings.approval_timeout_seconds = 300
    pending = await core.submit(
        Conversation("telegram", str(OWNER), "work"), "run exactly: echo hi ."
    )
    await eventually(lambda: bool(core.pending_approvals("telegram")))
    await feed(channel, 3, "/stop")
    assert core.job(pending.id).status is JobStatus.CANCELLED
    assert core.store.approval_audit(pending.id)[0].outcome == "cancelled"


def test_configuration_requires_allowlist_and_positive_unique_ids():
    with pytest.raises(ValidationError):
        TelegramSettings(enabled=True)
    for ids in ([0], [-1], [OWNER, OWNER]):
        with pytest.raises(ValidationError):
            TelegramSettings(allowed_user_ids=ids)


async def test_chat_approval_stays_private_to_its_owner(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start", user=OWNER)
    await feed(channel, 2, "/start", user=OTHER)
    await feed(channel, 3, "/new", user=OTHER)
    await feed(channel, 4, "run exactly: echo hi .", user=OTHER)
    await eventually(lambda: bool(channel._buttons))
    assert {chat for chat, _ in channel._buttons} == {OTHER}
    assert not any(m.reply_markup and m.chat.id == OWNER for m in session.sent)
    await feed(channel, 5, "/approvals", user=OWNER)
    assert session.sent[-1].text == "No pending approvals."
    button_message = next(m for m in session.sent if m.reply_markup and m.chat.id == OTHER)
    assert "From: this chat" in button_message.text
    await click(
        channel,
        6,
        button_message,
        button_message.reply_markup.inline_keyboard[0][0].callback_data,
        user=OTHER,
    )
    (job,) = core.store._conn.execute("SELECT id FROM jobs").fetchall()
    result = await core.wait(job[0], 5)
    assert result.answer == "hi"
    assert core.store.approval_audit(job[0])[0].actor == f"telegram:{OTHER}"


async def test_blocked_chat_does_not_starve_other_approvers(telegram):
    from aiogram.exceptions import TelegramForbiddenError

    core, channel, session, _ = telegram
    await feed(channel, 1, "/start", user=OWNER)
    await feed(channel, 2, "/start", user=OTHER)
    original = session.make_request

    async def blocked_by_owner(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == OWNER:
            raise TelegramForbiddenError(method=method, message="bot was blocked by the user")
        return await original(bot, method, timeout)

    session.make_request = blocked_by_owner
    job = await core.submit(Conversation("mcp", "test", "work"), "run exactly: echo hi .")
    await eventually(lambda: bool(channel._buttons))
    assert {chat for chat, _ in channel._buttons} == {OTHER}
    button_message = next(m for m in session.sent if m.reply_markup)
    assert "From: mcp:test" in button_message.text
    await core.cancel(job.conversation)


async def test_secret_is_redacted_before_truncation_and_blank_text_is_sent(telegram):
    from acp_gateway.log import register_secret

    _, channel, session, _ = telegram
    leaked = "S3CRETVALUE-" + "abcdefghijklmnop"
    register_secret(leaked)
    approval = type(
        "A",
        (),
        {
            "conversation": Conversation("mcp", "t", "work"),
            "job_id": "job",
            "id": "approval",
            "expires_at": datetime.now(UTC),
            "request": type(
                "R",
                (),
                {
                    "title": "x" * 1760 + leaked,
                    "raw_input": {},
                    "options": [
                        type("O", (), {"kind": "allow_once", "name": "Allow", "option_id": "o"})()
                    ],
                },
            )(),
        },
    )()
    await channel._approval(OWNER, approval)
    assert leaked[:10] not in session.sent[-1].text
    await channel._send(OWNER, " \n ")
    assert session.sent[-1].text == "(empty response)"


async def test_slow_result_delivery_does_not_block_approval_clicks(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    blocked = asyncio.Event()
    release = asyncio.Event()
    original = session.make_request

    async def slow(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.text == "slow saved answer":
            blocked.set()
            await release.wait()
        return await original(bot, method, timeout)

    session.make_request = slow
    channel._queue_delivery(OWNER, "slow saved answer")
    await asyncio.wait_for(blocked.wait(), 2)
    try:
        job = await core.submit(Conversation("mcp", "test", "work"), "run exactly: echo hi .")
        await eventually(lambda: bool(channel._buttons))
        message = next(m for m in session.sent if m.reply_markup)
        await click(channel, 2, message, message.reply_markup.inline_keyboard[0][0].callback_data)
        assert (await core.wait(job.id, 2)).answer == "hi"
        assert not release.is_set()
    finally:
        release.set()


async def test_agent_command_lists_available_routes_without_selection(telegram):
    _, channel, session, _ = telegram
    await feed(channel, 1, "/agent")
    assert "work: work" in session.sent[-1].text
    assert "Select with /agent ALIAS" in session.sent[-1].text


async def test_message_without_selected_dialogue_requires_explicit_new(telegram):
    core, channel, session, server = telegram
    await feed(channel, 1, "say pong")
    assert "No active dialogue" in session.sent[-1].text
    assert "/new" in session.sent[-1].text
    assert server.log == []
    assert core.sessions(Conversation("telegram", str(OWNER), "work")) == []


async def test_failed_new_route_keeps_previous_route_and_active_dialogue(telegram, monkeypatch):
    from acp_gateway.agents import AgentUnavailable

    core, channel, session, _ = telegram
    await feed(channel, 1, "/new work")
    conversation = Conversation("telegram", str(OWNER), "work")
    selected = core.active_session(conversation)

    async def failed(*args):
        raise AgentUnavailable("new target offline")

    monkeypatch.setattr(core, "new_session", failed)
    await feed(channel, 2, "/new other")
    assert "offline" in session.sent[-1].text
    assert core.store.channel_state(channel.namespace, f"agent:{OWNER}") == "work"
    assert core.active_session(conversation).id == selected.id


async def test_command_menu_is_registered_at_start_without_sending_messages(telegram):
    from aiogram.methods import SetChatMenuButton, SetMyCommands

    from acp_gateway.channels.telegram import COMMANDS

    _, _, session, _ = telegram
    await eventually(lambda: any(isinstance(c, SetChatMenuButton) for c in session.calls))
    menus = [c for c in session.calls if isinstance(c, SetMyCommands)]
    assert {c.language_code for c in menus} == {"", "ru"}
    for menu in menus:
        assert menu.scope.type == "all_private_chats"
        assert [c.command for c in menu.commands] == [row[0] for row in COMMANDS]
    assert (
        next(c for c in session.calls if isinstance(c, SetChatMenuButton)).menu_button.type
        == "commands"
    )
    assert not session.sent


async def test_menu_failure_retries_and_cancellation_is_not_swallowed(monkeypatch):
    from unittest.mock import AsyncMock

    from acp_gateway.channels import telegram as module

    session = FakeSession()
    channel = TelegramChannel(TelegramSettings(), SecretStr(TOKEN), bot=Bot(TOKEN, session=session))
    register = AsyncMock(side_effect=[RuntimeError("offline"), None])
    pause = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(channel, "_register_commands", register)
    monkeypatch.setattr(module.asyncio, "sleep", pause)
    try:
        with pytest.raises(asyncio.CancelledError):
            await channel._menus()
        assert register.await_count == 2
        assert [call.args[0] for call in pause.await_args_list] == [60, 3600]
        assert not session.sent
    finally:
        await channel.bot.session.close()


@pytest.mark.parametrize(
    "online,enabled,advertised,ready,failure,expected",
    [
        (True, True, True, False, None, "not checked yet; PC online"),
        (True, True, True, True, None, "ready"),
        (False, True, True, True, None, "PC offline"),
        (False, False, True, False, None, "PC access disabled"),
        (True, True, False, False, None, "agent not announced by PC"),
        (
            True,
            True,
            True,
            False,
            {"code": "open_failed"},
            "connection needs checking (open_failed)",
        ),
    ],
)
async def test_status_distinguishes_pc_link_from_lazy_acp(
    online, enabled, advertised, ready, failure, expected
):
    from types import SimpleNamespace

    session = FakeSession()
    profile = SimpleNamespace(backend="connector", computer_id="home", alias="hermes")
    client = SimpleNamespace(profile=profile, connected=ready)
    relay = SimpleNamespace(
        route_status=lambda alias: {
            "epoch": "test",
            "active_streams": 0,
            "capacity_available": True,
            "last_stream_error": failure,
        }
    )
    dispatcher = SimpleNamespace(
        status=lambda: [
            {
                "computer_id": "home",
                "enabled": enabled,
                "connected": online,
                "advertised_agents": ["hermes"] if advertised else [],
            }
        ],
        active={"home": SimpleNamespace(relay=relay)},
    )
    channel = TelegramChannel(
        TelegramSettings(), SecretStr(TOKEN), bot=Bot(TOKEN, session=session), dispatcher=dispatcher
    )
    channel.core = SimpleNamespace(
        agents={"home/hermes": client},
        channels={"cli": SimpleNamespace(connected=False), "mcp": SimpleNamespace(connected=True)},
        running_jobs=lambda: [],
    )
    try:
        await channel._command(OWNER, "/status")
        text = session.sent[-1].text
        assert f"home/hermes: {expected}" in text
        assert "cli" not in text and "unavailable" not in text and "disconnected" not in text
        assert "MCP: serving" in text
        assert len(text.splitlines()) == 4
    finally:
        await channel.bot.session.close()


async def test_local_status_does_not_connect_to_agent(telegram):
    _, channel, session, server = telegram
    await feed(channel, 1, "/status")
    assert "work: not initialized" in session.sent[-1].text
    assert not server.log
