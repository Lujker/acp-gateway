"""Result actions select exact owned context; neither action replays a prompt."""

import asyncio

import pytest
from test_telegram import OTHER, OWNER, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button
from test_telegram_delivery import pause_sender, saved

from acp_gateway.channels.telegram_format import plain_html
from acp_gateway.core import GatewayError
from acp_gateway.storage import Conversation, JobStatus
from fakes.telegram import answer_body


def finished(core, handle="result"):
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, handle, "/original-folder")
    job = core.store.add_job(handle, record.id)
    core.store.finish_job(job.id, JobStatus.COMPLETED, answer="Saved answer")
    return core.job(job.id)


@pytest.mark.parametrize("action", ["Сделать активным", "Новый диалог"])
async def test_result_action_is_private_once_only_and_does_not_replay(telegram, action):
    core, channel, session, server = telegram
    job = finished(core)
    newer = core.store.add_session(job.conversation, "current", "/other-folder")
    await channel._send_answer(OWNER, job.answer, job)
    result = session.sent[-1]
    data = button(result, action).callback_data
    await click(channel, 1, result, data, user=OTHER)
    await click(channel, 2, result.model_copy(update={"message_id": 999}), data)
    assert core.active_session(job.conversation).id == newer.id and not server.log
    await click(channel, 3, result, data)
    selected = core.active_session(job.conversation)
    if action == "Сделать активным":
        assert selected.id == job.session_id and not server.log
    else:
        assert selected.id not in {job.session_id, newer.id}
        assert selected.cwd == "/original-folder"
        assert "Предыдущий контекст не переносится" in session.sent[-1].text
        assert sum("new_session" in entry for entry in server.log) == 1
    revision = core.store.active_revision(job.conversation)
    await click(channel, 4, result, data)
    assert session.calls[-1].show_alert
    assert core.store.active_revision(job.conversation) == revision
    assert len(core.job_history("telegram", str(OWNER))["jobs"]) == 1
    assert answer_body(result.text) == "Saved answer"


@pytest.mark.parametrize("action", ["Сделать активным", "Новый диалог"])
@pytest.mark.parametrize("change", ["route", "selection"])
async def test_old_result_controls_cannot_override_newer_selection(telegram, action, change):
    core, channel, session, server = telegram
    job = finished(core)
    await channel._send_answer(OWNER, job.answer, job)
    result = session.sent[-1]
    data = button(result, action).callback_data
    if change == "route":
        channel._set_route(OWNER, "work")
    else:
        core.switch_dialogue(job.conversation, job.session_id)
    revision = core.store.active_revision(job.conversation)
    await click(channel, 1, result, data)
    assert session.calls[-1].show_alert and not server.log
    assert core.store.active_revision(job.conversation) == revision


async def test_automatic_long_result_has_dialogue_actions_only_on_last_fragment(telegram):
    core, channel, session, server = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    job = saved(core, channel, "A" * 2000 + "B")
    for _ in range(2):
        await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    fragments = [m for m in session.sent if "📄 Ответ ·" in m.text]
    assert len(fragments) == 2
    assert (
        "".join(plain_html(answer_body(m.text)).split("\n\n", 1)[1] for m in fragments)
        == "A" * 2000 + "B"
    )
    assert not any(
        "Новый диалог" in item.text
        for row in fragments[0].reply_markup.inline_keyboard
        for item in row
    )
    assert button(fragments[1], "Сделать активным") and button(fragments[1], "Новый диалог")
    assert core.store.deliveries.get(channel.namespace, job.id)["state"] == "delivered"
    assert not server.log


async def test_result_from_removed_route_keeps_result_controls_without_dialogue_actions(telegram):
    core, channel, session, _ = telegram
    job = finished(core)
    client = core.agents.pop("work")
    try:
        await channel._send_answer(OWNER, job.answer, job)
        result = session.sent[-1]
        assert button(result, "Результат")
        assert not any(
            "диалог" in item.text for row in result.reply_markup.inline_keyboard for item in row
        )
    finally:
        core.agents["work"] = client


async def test_unrestorable_result_offers_new_but_cannot_select_the_old_dialogue(
    telegram, monkeypatch
):
    core, channel, session, server = telegram
    job = finished(core)
    newer = core.store.add_session(job.conversation, "current", "/work")
    monkeypatch.setattr(
        core.agent("work"), "session_availability", lambda sid: "restore-unavailable"
    )
    await channel._send_answer(OWNER, job.answer, job)
    result = session.sent[-1]
    assert button(result, "Новый диалог")
    assert not any(
        "Сделать активным" in item.text
        for row in result.reply_markup.inline_keyboard
        for item in row
    )
    with pytest.raises(GatewayError, match="нельзя восстановить"):
        await channel._command(OWNER, f"/open_dialogue {job.id}")
    assert core.active_session(job.conversation).id == newer.id and not server.log


async def test_new_from_result_does_not_override_route_changed_during_rpc(telegram, monkeypatch):
    core, channel, session, server = telegram
    job = finished(core)
    await channel._send_answer(OWNER, job.answer, job)
    result = session.sent[-1]
    original = core.new_session

    async def changed(conv, **kwargs):
        channel._set_route(OWNER, "work")
        return await original(conv, **kwargs)

    monkeypatch.setattr(core, "new_session", changed)
    await click(channel, 1, result, button(result, "Новый диалог").callback_data)
    assert "Ваш выбор изменился" in session.sent[-1].text
    assert core.store.channel_state(channel.namespace, f"route_revision:{OWNER}") == 1
    assert len(core.job_history("telegram", str(OWNER))["jobs"]) == 1
    assert sum("new_session" in entry for entry in server.log) == 1


async def test_new_from_result_keeps_newer_dialogue_when_selection_changes_during_rpc(
    telegram, monkeypatch
):
    core, channel, session, _ = telegram
    job = finished(core)
    newest = core.store.add_session(job.conversation, "newest", "/work", activate=False)
    await channel._send_answer(OWNER, job.answer, job)
    result = session.sent[-1]
    client = core.agent("work")
    original = client.new_session
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(cwd):
        started.set()
        await release.wait()
        return await original(cwd)

    monkeypatch.setattr(client, "new_session", delayed)
    task = asyncio.create_task(
        click(channel, 1, result, button(result, "Новый диалог").callback_data)
    )
    await started.wait()
    core.switch_dialogue(job.conversation, newest.id)
    release.set()
    await task
    assert core.active_session(job.conversation).id == newest.id
    assert len(core.sessions(job.conversation)) == 3
    assert len(core.job_history("telegram", str(OWNER))["jobs"]) == 1


@pytest.mark.parametrize("label", ["Сделать активным", "Новый диалог"])
async def test_result_action_rechecks_selection_after_callback_ack(telegram, label):
    from aiogram.methods import AnswerCallbackQuery

    core, channel, session, server = telegram
    job = finished(core)
    newest = core.store.add_session(job.conversation, "newest", "/work", activate=False)
    await channel._send_answer(OWNER, job.answer, job)
    result = session.sent[-1]
    original = session.make_request

    async def changed(bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            core.switch_dialogue(job.conversation, newest.id)
        return await original(bot, method, timeout)

    session.make_request = changed
    await click(channel, 1, result, button(result, label).callback_data)
    assert core.active_session(job.conversation).id == newest.id
    assert "Диалог или агент изменился" in session.sent[-1].text
    assert not server.log and len(core.sessions(job.conversation)) == 2


async def test_new_from_result_preserves_selection_on_another_route_during_rpc(
    telegram, monkeypatch, tmp_path
):
    from pydantic import SecretStr
    from test_telegram import SECRET

    from acp_gateway.agents import AgentClient

    core, channel, session, _ = telegram
    job = finished(core)
    client = core.agent("work")
    other = AgentClient(
        client.profile.model_copy(update={"alias": "other"}),
        SecretStr(SECRET),
        pin_dir=tmp_path / "other-pins",
    )
    core.agents["other"] = other
    conv = Conversation("telegram", str(OWNER), "other")
    core.store.add_session(conv, "other-first", "/other")
    newest = core.store.add_session(conv, "other-newest", "/other", activate=False)
    channel._set_route(OWNER, "other")
    await channel._send_answer(OWNER, job.answer, job)
    result = session.sent[-1]
    original = client.new_session
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(cwd):
        entered.set()
        await release.wait()
        return await original(cwd)

    monkeypatch.setattr(client, "new_session", delayed)
    task = asyncio.create_task(
        click(channel, 1, result, button(result, "Новый диалог").callback_data)
    )
    await entered.wait()
    core.switch_dialogue(conv, newest.id)
    release.set()
    await task
    assert channel._conversation(OWNER) == conv
    assert core.active_session(conv).id == newest.id
    assert "Ваш выбор изменился" in session.sent[-1].text
    assert len(core.sessions(job.conversation)) == 2
