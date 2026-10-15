"""Recent and favorite shortcuts are private, bounded and never submit prompts."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from test_telegram import OTHER, OWNER, TOKEN, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.core import GatewayCore, GatewayError
from acp_gateway.storage import Conversation, Store


def add(core, handle, agent="work", key=OWNER, channel="telegram"):
    record = core.store.add_session(Conversation(channel, str(key), agent), handle, "/work")
    core.store.set_session_title(record.id, handle)
    return record


async def test_recent_metadata_is_bounded_sorted_and_chat_channel_scoped(telegram, monkeypatch):
    core, channel, session, server = telegram
    monkeypatch.setitem(core.agents, "other", core.agent("work"))
    own = [add(core, f"own-{i}", agent="work" if i % 2 else "other") for i in range(12)]
    foreign = add(core, "FOREIGN", key=OTHER)
    add(core, "CLI-PRIVATE", channel="cli")
    core.store._conn.execute("UPDATE sessions SET last_used_at = '2026-10-15T10:00:00+00:00'")
    recent = core.recent_dialogues("telegram", str(OWNER))
    assert [r.id for r in recent] == [r.id for r in reversed(own[-10:])]
    assert not hasattr(recent[0], "answer")
    for limit in [0, 51, True, "10"]:
        with pytest.raises(GatewayError):
            core.recent_dialogues("telegram", str(OWNER), limit=limit)
    with pytest.raises(GatewayError):
        core.owned_dialogue(foreign.id, channel="telegram", key=str(OWNER))
    await feed(channel, 1, "/recent")
    assert "FOREIGN" not in session.sent[-1].text and "CLI-PRIVATE" not in session.sent[-1].text
    assert "other" in session.sent[-1].text and "work" in session.sent[-1].text
    assert not server.log


async def test_recent_preview_does_not_select_and_explicit_select_targets_exact_route(
    telegram, monkeypatch
):
    core, channel, session, server = telegram
    monkeypatch.setitem(core.agents, "other", core.agent("work"))
    target = add(core, "Target", "other")
    current = add(core, "Current")
    channel._set_route(OWNER, "work")
    revision = core.store.active_revision(current.conversation)
    await feed(channel, 1, "/recent")
    listing = session.sent[-1]
    await click(channel, 2, listing, button(listing, "Target").callback_data)
    preview = session.sent[-1]
    assert "Предпросмотр не меняет" in preview.text
    assert channel._conversation(OWNER).agent == "work"
    assert core.store.active_revision(current.conversation) == revision
    data = button(preview, "Выбрать диалог").callback_data
    await click(channel, 3, preview, data)
    assert channel._conversation(OWNER) == target.conversation
    assert core.active_session(target.conversation).id == target.id
    rev = core.store.active_revision(target.conversation)
    await click(channel, 4, preview, data)
    assert session.calls[-1].show_alert and core.store.active_revision(target.conversation) == rev
    assert not server.log


@pytest.mark.parametrize("change", ["route", "selection"])
async def test_recent_select_rejects_a_newer_choice(telegram, change):
    core, channel, session, server = telegram
    target = add(core, "Target")
    await feed(channel, 1, f"/recent_dialogue {target.id}")
    preview = session.sent[-1]
    data = button(preview, "Выбрать диалог").callback_data
    if change == "route":
        channel._set_route(OWNER, "work")
    else:
        core.switch_dialogue(target.conversation, target.id)
    await click(channel, 2, preview, data)
    assert session.calls[-1].show_alert and not server.log


async def test_removed_route_retains_local_name_without_a_select_action(telegram):
    core, channel, session, server = telegram
    removed = add(core, "Old host name", "retired")
    core.store.set_channel_state(
        "dialogue_display_titles", core._dialogue_title_key(removed), "Saved local name"
    )
    await feed(channel, 1, "/recent")
    assert "Saved local name" in session.sent[-1].text
    await feed(channel, 2, f"/recent_dialogue {removed.id}")
    preview = session.sent[-1]
    assert "маршрут удалён" in preview.text
    assert not any(
        "Выбрать диалог" in b.text for row in preview.reply_markup.inline_keyboard for b in row
    )
    with pytest.raises(GatewayError, match="недоступен"):
        await channel._command(OWNER, f"/select_recent {removed.id}")
    assert not server.log


async def test_favorites_can_be_added_by_button_are_private_and_do_not_change_selection(telegram):
    core, channel, session, server = telegram
    current = add(core, "Current")
    rev = core.store.active_revision(current.conversation)
    await feed(channel, 1, "/agent")
    agents = session.sent[-1]
    await click(channel, 2, agents, button(agents, "☆ Добавить").callback_data)
    assert channel._favorites(OWNER)["routes"] == ["work"]
    assert channel._favorites(OTHER)["routes"] == []
    assert core.store.active_revision(current.conversation) == rev
    assert button(session.sent[-1], "Выбрать work")
    assert not server.log


async def test_old_favorite_mutation_cannot_overwrite_a_newer_list(telegram):
    _, channel, session, server = telegram
    await feed(channel, 1, "/agent")
    old = session.sent[-1]
    action = button(old, "☆ Добавить").callback_data
    await channel._command(OWNER, "/favorite add work 0")
    await channel._command(OWNER, "/favorite remove work 1")
    await click(channel, 2, old, action)
    assert channel._favorites(OWNER)["routes"] == []
    updated = next(m for m in session.sent if m.message_id == old.message_id)
    assert "Избранное изменилось" in updated.text and not server.log


@pytest.mark.parametrize("change", ["route", "selection", "favorites"])
async def test_favorite_select_is_once_only_and_fenced(telegram, change):
    core, channel, session, server = telegram
    current = add(core, "Current")
    await feed(channel, 1, "/favorite add work 0")
    favorite = session.sent[-1]
    data = button(favorite, "Выбрать work").callback_data
    if change == "route":
        channel._set_route(OWNER, "work")
    elif change == "selection":
        core.switch_dialogue(current.conversation, current.id)
    else:
        await channel._command(OWNER, "/favorite remove work 1")
    rev = core.store.active_revision(current.conversation)
    route = channel.core.store.channel_state(channel.namespace, f"agent:{OWNER}")
    await click(channel, 2, favorite, data)
    assert core.store.active_revision(current.conversation) == rev and not server.log
    assert channel.core.store.channel_state(channel.namespace, f"agent:{OWNER}") == route


async def test_offline_and_removed_favorites_are_removable_without_selection(telegram, monkeypatch):
    core, channel, session, server = telegram
    await feed(channel, 1, "/favorite add work 0")
    client = core.agent("work")
    monkeypatch.setattr(
        client,
        "profile",
        client.profile.model_copy(update={"backend": "connector", "computer_id": "pc"}),
    )
    channel.connector_dispatcher = SimpleNamespace(
        status=lambda: [
            {
                "computer_id": "pc",
                "enabled": True,
                "connected": False,
                "advertised_agents": [],
            }
        ]
    )
    await feed(channel, 2, "/favorites")
    assert "ПК не подключен" in session.sent[-1].text
    assert not any(
        "Выбрать work" in b.text
        for row in session.sent[-1].reply_markup.inline_keyboard
        for b in row
    )
    with pytest.raises(GatewayError, match="недоступен"):
        await channel._command(OWNER, "/select_favorite work 1")
    removed = core.agents.pop("work")
    try:
        await channel._command(OWNER, "/favorites")
        assert "маршрут удалён" in session.sent[-1].text
        await channel._command(OWNER, "/favorite remove work 1")
        assert channel._favorites(OWNER)["routes"] == []
    finally:
        core.agents["work"] = removed
    assert not server.log


async def test_favorites_survive_sqlite_reopen_and_are_bounded(telegram, tmp_path, monkeypatch):
    core, channel, _, server = telegram
    for i in range(20):
        alias = f"route-{i}"
        monkeypatch.setitem(core.agents, alias, core.agent("work"))
        await channel._command(OWNER, f"/favorite add {alias} {i}")
    with pytest.raises(GatewayError, match="до 20"):
        await channel._command(OWNER, "/favorite add work 20")
    reopened = Store.open(tmp_path / "gateway.db")
    try:
        fresh = TelegramChannel(channel.settings, SecretStr(TOKEN), bot=channel.bot)
        fresh.core = GatewayCore(reopened, core.agents, policy=core.policy.settings)
        assert len(fresh._favorites(OWNER)["routes"]) == 20
        assert fresh._favorites(OTHER)["routes"] == []
    finally:
        reopened.close()
    assert not server.log


async def test_favorite_select_preserves_its_dialogue_and_is_once_only(telegram):
    core, channel, session, server = telegram
    current = add(core, "Current")
    await feed(channel, 1, "/favorite add work 0")
    listing = session.sent[-1]
    action = button(listing, "Выбрать work").callback_data
    await click(channel, 2, listing, action)
    assert channel._conversation(OWNER) == current.conversation
    assert core.active_session(current.conversation).id == current.id
    revision = core.store.channel_state(channel.namespace, f"route_revision:{OWNER}")
    await click(channel, 3, listing, action)
    assert session.calls[-1].show_alert
    assert core.store.channel_state(channel.namespace, f"route_revision:{OWNER}") == revision
    assert not server.log


async def test_recent_preview_and_select_refuse_foreign_chat_without_disclosing_metadata(telegram):
    core, channel, session, server = telegram
    private = add(core, "PRIVATE TARGET")
    await feed(channel, 1, f"/recent_dialogue {private.id}", user=OTHER)
    assert "PRIVATE TARGET" not in session.sent[-1].text
    await feed(channel, 2, f"/select_recent {private.id}", user=OTHER)
    assert "PRIVATE TARGET" not in session.sent[-1].text
    assert core.store.channel_state(channel.namespace, f"agent:{OTHER}") is None
    assert not server.log
