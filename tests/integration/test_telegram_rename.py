# ruff: noqa: RUF001
"""Bound name input never becomes a prompt, including after a SQLite reopen."""

import pytest
from pydantic import SecretStr
from test_telegram import OTHER, OWNER, TOKEN, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.core import GatewayCore
from acp_gateway.storage import Conversation, Store


def dialogue(core, key=OWNER, handle="rename"):
    return core.store.add_session(Conversation("telegram", str(key), "work"), handle, "/work")


async def begin(core, channel, session, number=1):
    record = dialogue(core)
    await feed(channel, number, f"/dialogue {record.id}")
    card = session.sent[-1]
    await click(channel, number + 1, card, button(card, "Переименовать").callback_data)
    return record, session.sent[-1]


async def test_rename_button_consumes_name_preserves_selection_and_host_title(telegram):
    core, channel, session, server = telegram
    record, card = await begin(core, channel, session)
    revision = core.store.active_revision(record.conversation)
    assert "5 минут" in card.text and "следующим сообщением" in card.text
    await feed(channel, 3, "Research <draft>")
    assert "Research &lt;draft&gt;" in session.sent[-1].text
    assert core.active_session(record.conversation).title == "Research <draft>"
    assert core.store.session(record.id).title is None
    assert core.store.active_revision(record.conversation) == revision
    assert channel._rename_input(OWNER) is None
    assert not core.job_history("telegram", str(OWNER))["jobs"] and not server.log


@pytest.mark.parametrize("method", ["button", "command", "navigation"])
async def test_rename_cancel_keeps_title_and_clears_input(telegram, method):
    core, channel, session, server = telegram
    record, card = await begin(core, channel, session)
    if method == "button":
        await click(channel, 3, card, button(card, "Отмена").callback_data)
    else:
        await feed(channel, 3, "/cancel" if method == "command" else "/menu")
    assert channel._rename_input(OWNER) is None
    assert core.active_session(record.conversation).title is None
    assert not server.log


@pytest.mark.parametrize("change", ["route", "selection", "expired", "deleted"])
async def test_stale_name_is_discarded_without_a_prompt(telegram, change):
    core, channel, session, server = telegram
    record, _ = await begin(core, channel, session)
    if change == "route":
        channel._set_route(OWNER, "work")
    elif change == "selection":
        core.switch_dialogue(record.conversation, record.id)
    elif change == "deleted":
        core.store._conn.execute("DELETE FROM sessions WHERE id = ?", (record.id,))
    else:
        pending = channel._rename_input(OWNER)
        pending["expires"] = 0
        core.store.set_channel_state(channel.namespace, f"rename_input:{OWNER}", pending)
    await feed(channel, 3, "do not execute this name")
    assert "Это сообщение не отправлено агенту" in session.sent[-1].text
    assert channel._rename_input(OWNER) is None
    assert not core.job_history("telegram", str(OWNER))["jobs"] and not server.log


async def test_invalid_name_keeps_input_and_accepts_a_correction(telegram):
    core, channel, session, server = telegram
    record, _ = await begin(core, channel, session)
    await feed(channel, 3, "x" * 121)
    assert channel._rename_input(OWNER)
    assert core.active_session(record.conversation).title is None
    await feed(channel, 4, "Corrected name")
    assert core.active_session(record.conversation).title == "Corrected name"
    assert not server.log


async def test_rename_controls_cannot_cross_chat_message_or_newer_selection(telegram):
    core, channel, session, server = telegram
    record = dialogue(core)
    await feed(channel, 1, f"/dialogue {record.id}")
    card = session.sent[-1]
    action = button(card, "Переименовать").callback_data
    await click(channel, 2, card, action, user=OTHER)
    await click(channel, 3, card.model_copy(update={"message_id": 999}), action)
    assert channel._rename_input(OWNER) is None
    core.switch_dialogue(record.conversation, record.id)
    await click(channel, 4, card, action)
    assert session.calls[-1].show_alert and channel._rename_input(OWNER) is None
    assert not server.log


async def test_old_cancel_cannot_discard_a_newer_name_input(telegram):
    core, channel, session, server = telegram
    record, card = await begin(core, channel, session)
    old = button(card, "Отмена").callback_data
    await feed(channel, 3, f"/rename_hint {record.id}")
    latest = channel._rename_input(OWNER)
    await click(channel, 4, card, old)
    assert channel._rename_input(OWNER) == latest
    assert not server.log


async def test_name_input_survives_sqlite_reopen_and_fresh_channel(telegram, tmp_path):
    core, channel, session, server = telegram
    record, _ = await begin(core, channel, session)
    reopened = Store.open(tmp_path / "gateway.db")
    try:
        reloaded = GatewayCore(reopened, core.agents, policy=core.policy.settings)
        fresh = TelegramChannel(channel.settings, SecretStr(TOKEN), bot=channel.bot)
        fresh.core = reloaded
        assert fresh._rename_input(OWNER)["id"] == record.id
        await fresh._command(OWNER, "Saved after restart")
        assert reloaded.active_session(record.conversation).title == "Saved after restart"
        assert fresh._rename_input(OWNER) is None
        assert not reloaded.job_history("telegram", str(OWNER))["jobs"]
        assert not server.log
    finally:
        reopened.close()


async def test_rename_nonselected_dialogue_preserves_page_and_active_choice(telegram):
    core, channel, session, server = telegram
    old = dialogue(core)
    for i in range(10):
        current = dialogue(core, handle=f"newer-{i}")
    revision = core.store.active_revision(old.conversation)
    await feed(channel, 1, f"/rename_hint {old.id} 2")
    await feed(channel, 2, "Old dialogue name")
    card = session.sent[-1]
    await click(channel, 3, card, button(card, "← Диалог").callback_data)
    assert f"Диалог #{old.id}" in session.sent[-1].text
    assert "Не выбран для сообщений" in session.sent[-1].text
    assert core.active_session(old.conversation).id == current.id
    assert core.store.active_revision(old.conversation) == revision
    assert not server.log


async def test_rename_prompt_send_failure_keeps_name_out_of_agent_input(telegram):
    from aiogram.exceptions import TelegramNetworkError

    core, channel, session, server = telegram
    record = dialogue(core)
    session.failures.append(lambda method: TelegramNetworkError(method=method, message="timeout"))
    await feed(channel, 1, f"/rename_hint {record.id}")
    assert channel._rename_input(OWNER)
    await feed(channel, 2, "Name after lost UI")
    assert core.active_session(record.conversation).title == "Name after lost UI"
    assert not core.job_history("telegram", str(OWNER))["jobs"] and not server.log


async def test_foreign_chat_cannot_begin_input_for_an_owned_dialogue(telegram):
    core, channel, session, server = telegram
    record, _ = await begin(core, channel, session)
    pending = channel._rename_input(OWNER)
    await feed(channel, 3, f"/rename_hint {record.id}", user=OTHER)
    assert channel._rename_input(OTHER) is None
    assert channel._rename_input(OWNER) == pending
    assert core.active_session(record.conversation).title is None and not server.log
