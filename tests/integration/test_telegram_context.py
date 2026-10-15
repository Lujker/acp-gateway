"""A durable, independent context message tracks only this chat's selected route."""

import asyncio
import secrets
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import EditMessageText
from pydantic import SecretStr
from test_telegram import OTHER, OWNER, TOKEN, approval_message, click, eventually, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.core import GatewayCore
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, Store


def record(core, handle="context", key=OWNER):
    return core.store.add_session(Conversation("telegram", str(key), "work"), handle, "/work")


def card(session, mid):
    return next(m for m in session.sent if m.message_id == mid)


async def test_context_updates_same_message_after_rename_and_selection_without_rpc(telegram):
    core, channel, session, server = telegram
    first = record(core)
    await feed(channel, 1, "/context")
    mid = session.sent[-1].message_id
    assert f"Диалог: #{first.id}" in card(session, mid).text
    assert "Папка: /work" in card(session, mid).text
    await feed(channel, 2, f"/rename {first.id} Research")
    await eventually(lambda: "Research" in card(session, mid).text)
    newer = record(core, "newer")
    await feed(channel, 3, "/menu")
    await eventually(lambda: f"Диалог: #{newer.id}" in card(session, mid).text)
    assert sum(m.text.startswith("Текущий контекст") for m in session.sent) == 1
    assert not server.log


async def test_context_is_private_and_redacts_before_rendering(telegram):
    core, channel, session, server = telegram
    owner = record(core)
    record(core, "foreign", OTHER)
    secret = secrets.token_hex(40)
    register_secret(secret)
    core.rename_dialogue(owner.conversation, owner.id, "<private> " + secret)
    await feed(channel, 1, "/context")
    assert "&lt;private&gt;" in session.sent[-1].text and secret not in session.sent[-1].text
    await feed(channel, 2, "/context", user=OTHER)
    assert "private" not in session.sent[-1].text and secret not in session.sent[-1].text
    assert session.sent[-1].chat.id == OTHER and not server.log


@pytest.mark.parametrize(
    "enabled,connected,advertised,expected",
    [
        (True, True, True, "не проверена"),
        (True, True, False, "не объявлен коннектором"),
        (True, False, False, "не подключен"),
        (False, True, True, "доступ выключен"),
    ],
)
async def test_connector_snapshot_does_not_probe_or_invent_readiness(
    telegram, monkeypatch, enabled, connected, advertised, expected
):
    core, channel, session, server = telegram
    client = core.agent("work")
    monkeypatch.setattr(
        client,
        "profile",
        client.profile.model_copy(
            update={
                "backend": "connector",
                "computer_id": "home",
            }
        ),
    )
    channel.connector_dispatcher = SimpleNamespace(
        status=lambda: [
            {
                "computer_id": "home",
                "display_name": "Home PC",
                "enabled": enabled,
                "connected": connected,
                "advertised_agents": ["work"] if advertised else [],
            }
        ]
    )
    await feed(channel, 1, "/context")
    assert "Home PC" in session.sent[-1].text and expected in session.sent[-1].text
    assert "Папка новых диалогов:" in session.sent[-1].text and not server.log


async def test_context_detects_connector_changes_and_removed_agent(telegram, monkeypatch):
    core, channel, session, server = telegram
    client = core.agent("work")
    monkeypatch.setattr(
        client,
        "profile",
        client.profile.model_copy(
            update={
                "backend": "connector",
                "computer_id": "home",
            }
        ),
    )
    pc = {
        "computer_id": "home",
        "display_name": "Home",
        "enabled": True,
        "connected": True,
        "advertised_agents": ["work"],
    }
    channel.connector_dispatcher = SimpleNamespace(status=lambda: [pc])
    await feed(channel, 1, "/context")
    mid = session.sent[-1].message_id
    pc["connected"] = False
    channel._context_wake.set()
    await eventually(lambda: "не подключен" in card(session, mid).text)
    channel.core.store.set_channel_state(channel.namespace, f"agent:{OWNER}", "retired")
    channel._context_wake.set()
    await eventually(lambda: "Маршрут недоступен" in card(session, mid).text)
    assert "retired" in card(session, mid).text and not server.log


async def test_context_state_survives_sqlite_reopen_without_another_send(telegram, tmp_path):
    core, channel, session, server = telegram
    old = record(core)
    await feed(channel, 1, "/context")
    mid = session.sent[-1].message_id
    reopened = Store.open(tmp_path / "gateway.db")
    try:
        fresh = TelegramChannel(channel.settings, SecretStr(TOKEN), bot=channel.bot)
        fresh.core = GatewayCore(reopened, core.agents, policy=core.policy.settings)
        before = sum(isinstance(c, EditMessageText) for c in session.calls)
        await fresh._show_context(OWNER)
        assert sum(isinstance(c, EditMessageText) for c in session.calls) == before + 1
        # Same text still receives new callback bindings after a restart.
        refresh = button(card(session, mid), "Обновить контекст").callback_data
        assert fresh._panels.take(refresh, OWNER, mid, fresh._ui_context(OWNER)) == "/context"
        fresh.core.rename_dialogue(old.conversation, old.id, "Restored card")
        await fresh._show_context(OWNER)
        assert len(session.sent) == 1 and "Restored card" in card(session, mid).text
        assert not server.log
    finally:
        reopened.close()


async def test_ambiguous_context_send_is_not_replayed_automatically(telegram):
    _, channel, session, _ = telegram
    session.failures.append(lambda method: TelegramNetworkError(method=method, message="timeout"))
    await feed(channel, 1, "/context")
    before = len(session.calls)
    await channel._show_context(OWNER)
    assert len(session.calls) == before
    assert (
        channel.core.store.channel_state(channel.namespace, f"context_card:{OWNER}")["state"]
        == "unknown"
    )
    await feed(channel, 2, "/context")
    assert len(session.sent) == 1


async def test_context_edit_429_waits_and_missing_message_can_be_recreated(telegram):
    core, channel, session, _ = telegram
    old = record(core)
    await feed(channel, 1, "/context")
    core.rename_dialogue(old.conversation, old.id, "Updated")
    session.edit_failures.append(
        lambda method: TelegramRetryAfter(method=method, message="slow", retry_after=30)
    )
    await channel._show_context(OWNER)
    before = len(session.calls)
    await channel._show_context(OWNER)
    assert len(session.calls) == before
    session.edit_failures.append(
        lambda method: TelegramBadRequest(method=method, message="message to edit not found")
    )
    await channel._show_context(OWNER, explicit=True)
    assert len(session.sent) == 2 and "Updated" in session.sent[-1].text


async def test_blocked_context_edit_does_not_block_result_and_late_edit_is_serialized(telegram):
    core, channel, session, _ = telegram
    old = record(core)
    await feed(channel, 1, "/context")
    mid = session.sent[-1].message_id
    entered, release = asyncio.Event(), asyncio.Event()
    original = session.make_request

    async def delayed(bot, method, timeout=None):
        if isinstance(method, EditMessageText) and method.message_id == mid:
            entered.set()
            await release.wait()
        return await original(bot, method, timeout)

    session.make_request = delayed
    core.rename_dialogue(old.conversation, old.id, "Intermediate")
    channel._context_wake.set()
    await asyncio.wait_for(entered.wait(), 3)
    try:
        newest = await core.new_session(old.conversation)
        await feed(channel, 2, "run exactly: echo hi .")
        await eventually(lambda: bool(channel._buttons))
        approval = next(m for m in session.sent if approval_message(m))
        await click(channel, 3, approval, approval.reply_markup.inline_keyboard[0][0].callback_data)
        await eventually(lambda: any(m.text == "hi" for m in session.sent))
        release.set()
        channel._context_wake.set()
        await eventually(lambda: f"Диалог: #{newest.id}" in card(session, mid).text)
        assert core.active_session(old.conversation).id == newest.id
    finally:
        release.set()


async def test_context_button_keeps_navigation_and_context_messages_separate(telegram):
    core, channel, session, server = telegram
    record(core)
    await feed(channel, 1, "/menu")
    menu = session.sent[-1]
    assert "Папка: /work" in menu.text
    await click(channel, 2, menu, button(menu, "Текущий контекст").callback_data)
    assert len(session.sent) == 2
    assert session.sent[-1].message_id != menu.message_id
    assert session.sent[-1].text.startswith("Текущий контекст")
    assert not server.log


async def test_ambiguous_context_edit_warns_once_without_automatic_replay(telegram):
    core, channel, session, _ = telegram
    old = record(core)
    await feed(channel, 1, "/context")
    session.edit_failures.append(
        lambda method: TelegramNetworkError(method=method, message="ambiguous edit")
    )
    core.rename_dialogue(old.conversation, old.id, "New context")
    channel._context_wake.set()
    await eventually(lambda: any("может быть устаревшей" in m.text for m in session.sent))
    before = sum(isinstance(c, EditMessageText) for c in session.calls)
    await channel._show_context(OWNER)
    assert sum(isinstance(c, EditMessageText) for c in session.calls) == before
    assert sum("может быть устаревшей" in m.text for m in session.sent) == 1
