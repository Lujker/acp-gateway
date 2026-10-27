"""Destination snapshots must describe routing without changing it or probing agents."""

import asyncio

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText
from test_telegram import OWNER, click
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.channels.telegram_format import plain_html
from acp_gateway.storage import Conversation


async def test_preview_footer_names_actual_destination_not_previewed_dialogue(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    preview = core.store.add_session(conv, "preview", "/preview")
    selected = core.store.add_session(conv, "selected", "/selected")
    core.rename_dialogue(conv, preview.id, "Preview")
    core.rename_dialogue(conv, selected.id, "Selected")
    await channel._command(OWNER, f"/dialogue {preview.id}")
    text = plain_html(session.sent[-1].text)
    footer = text.split("────────────────────", 1)[1]
    assert f"Следующий текст → «Selected» #{selected.id}" in footer
    assert "Папка: /selected" in footer and "/preview" not in footer
    assert core.active_session(conv).id == selected.id
    assert "Выбор на момент обновления панели" in footer
    assert not server.log


async def test_form_footer_replaces_prompt_destination_even_for_other_dialogue(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    preview = core.store.add_session(conv, "rename", "/preview")
    selected = core.store.add_session(conv, "selected", "/selected")
    await channel._command(OWNER, f"/rename_hint {preview.id}")
    footer = plain_html(session.sent[-1].text).split("────────────────────", 1)[1]
    assert f"Следующий текст → название диалога #{preview.id}" in footer
    assert "/selected" not in footer
    assert core.active_session(conv).id == selected.id
    await channel._command(OWNER, "/cancel")
    assert f"«Без названия» #{selected.id}" in plain_html(session.sent[-1].text)
    assert not server.log


async def test_footer_uses_observation_and_never_claims_pending_model_is_applied(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "model", "/work")
    await channel._command(OWNER, "/menu")
    assert "модель неизвестна" in session.sent[-1].text
    core.store.models.observe(
        record.id,
        {
            "provider": "provider-a",
            "model": "model-a",
            "pending": None,
            "observation_source": "model.options",
        },
    )
    core.store.models.begin(record.id, "provider-b", "model-b", 1)
    await channel._command(OWNER, "/menu")
    text = plain_html(session.sent[-1].text)
    assert "provider-a · model-a (последнее наблюдение)" in text
    assert "Смена модели ещё не подтверждена" in text
    assert "model-b" not in text and not server.log


async def test_no_dialogue_or_removed_route_is_explicit_without_rpc(telegram):
    core, channel, session, server = telegram
    await channel._command(OWNER, "/menu")
    assert "Диалог не выбран; текст не отправится агенту" in session.sent[-1].text
    core.store.set_channel_state(channel.namespace, f"agent:{OWNER}", "removed")
    await channel._command(OWNER, "/menu")
    assert "Маршрут недоступен" in session.sent[-1].text
    assert not server.log


async def test_long_paths_do_not_split_dialogue_navigation_and_all_pages_are_reachable(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    for index in range(25):
        item = core.store.add_session(conv, f"page-{index}", "/work/" + "folder/" * 100)
        core.rename_dialogue(conv, item.id, "Title " + "x" * 110)
    selected = core.active_session(conv).id
    await channel._command(OWNER, "/sessions")
    panel = session.sent[-1]
    assert len(session.sent) == 1
    await click(channel, 1, panel, button(panel, "Далее").callback_data)
    panel = session.sent[-1]
    await click(channel, 2, panel, button(panel, "Далее").callback_data)
    panel = session.sent[-1]
    assert "Страница 3/3" in plain_html(panel.text)
    assert not any("Далее" in b.text for row in panel.reply_markup.inline_keyboard for b in row)
    await click(channel, 3, panel, button(panel, "← Назад").callback_data)
    panel = session.sent[-1]
    assert "Страница 2/3" in plain_html(panel.text)
    await click(channel, 4, panel, button(panel, "← Назад").callback_data)
    panel = session.sent[-1]
    assert "Страница 1/3" in plain_html(panel.text)
    assert len(session.sent) == 1 and core.active_session(conv).id == selected
    assert not server.log


async def test_old_panels_refresh_destination_without_reauthorizing_stale_actions(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    first = core.store.add_session(conv, "first", "/first")
    await channel._command(OWNER, "/menu")
    menu = session.sent[-1]
    old_new = button(menu, "Новый диалог").callback_data
    second = core.store.add_session(conv, "second", "/second")
    await channel._refresh_navigation(OWNER)
    text = plain_html(session.sent[-1].text)
    assert f"«Без названия» #{second.id}" in text and "/second" in text
    assert "Содержимое панели — до смены выбора" in text
    await click(channel, 1, menu, old_new)
    assert len(core.sessions(conv)) == 2
    assert core.active_session(conv).id != first.id
    assert len(session.sent) == 1 and not server.log


async def test_background_footer_cannot_overwrite_newer_navigation(telegram):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    core.store.add_session(conv, "first", "/first")
    await channel._command(OWNER, "/menu")
    panel = session.sent[-1]
    core.store.add_session(conv, "second", "/second")
    entered, release = asyncio.Event(), asyncio.Event()
    original = session.make_request

    async def delayed(bot, method, timeout=None):
        if isinstance(method, EditMessageText) and "Управление ACP Gateway" in method.text:
            entered.set()
            await release.wait()
        return await original(bot, method, timeout)

    session.make_request = delayed
    refresh = asyncio.create_task(channel._refresh_navigation(OWNER))
    await asyncio.wait_for(entered.wait(), 3)
    navigation = asyncio.create_task(
        click(channel, 1, panel, button(panel, "Агенты").callback_data)
    )
    try:
        release.set()
        await asyncio.gather(refresh, navigation)
        assert plain_html(session.sent[-1].text).startswith("🖥 Агенты")
        assert len(session.sent) == 1
    finally:
        release.set()
        await asyncio.gather(refresh, navigation, return_exceptions=True)


async def test_ambiguous_footer_edit_is_not_replayed_or_replaced_with_a_send(telegram):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    core.store.add_session(conv, "first", "/first")
    await channel._command(OWNER, "/menu")
    core.store.add_session(conv, "second", "/second")
    session.edit_failures.append(
        lambda method: TelegramNetworkError(method=method, message="ambiguous edit")
    )
    await channel._refresh_navigation(OWNER)
    calls = len(session.calls)
    await channel._refresh_navigation(OWNER)
    assert len(session.calls) == calls and len(session.sent) == 1
