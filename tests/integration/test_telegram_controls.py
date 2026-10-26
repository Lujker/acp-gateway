# ruff: noqa: RUF001
"""Inline user controls use the same private conversation and operation guards."""

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from test_telegram import OTHER, OWNER, click, eventually, feed
from test_telegram import telegram as telegram

from acp_gateway.channels.telegram_format import plain_html
from acp_gateway.storage import Conversation, JobStatus


def button(message, label):
    return next(
        item for row in message.reply_markup.inline_keyboard for item in row if label in item.text
    )


async def test_panel_selects_agent_and_creates_one_dialogue_for_double_click(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    home = session.sent[-1]
    await click(channel, 2, home, button(home, "Агенты").callback_data)
    agents = session.sent[-1]
    await click(channel, 3, agents, agents.reply_markup.inline_keyboard[0][0].callback_data)
    selected = session.sent[-1]
    action = button(selected, "Новый диалог").callback_data
    await click(channel, 4, selected, action)
    assert "Новый диалог #" in session.sent[-1].text
    await click(channel, 5, selected, action)
    assert len(core.sessions(Conversation("telegram", str(OWNER), "work"))) == 1
    assert any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in session.calls)


async def test_buttons_cannot_be_forwarded_to_another_user_message_or_changed_agent(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    home = session.sent[-1]
    new = button(home, "Новый диалог").callback_data
    await click(channel, 2, home, new, user=OTHER)
    assert not core.sessions(Conversation("telegram", str(OWNER), "work"))
    await click(channel, 3, home.model_copy(update={"message_id": 999}), new)
    assert not core.sessions(Conversation("telegram", str(OWNER), "work"))
    await feed(channel, 4, "/agent work")  # Same route selected again has a new revision.
    await click(channel, 5, home, new)
    assert not core.sessions(Conversation("telegram", str(OWNER), "work"))


async def test_dialogue_buttons_and_navigation_do_not_make_prompts(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    for index in range(12):
        core.store.add_session(conv, f"saved-{index}", "/work")
    await feed(channel, 1, "/sessions")
    page = session.sent[-1]
    await click(channel, 2, page, button(page, "Далее").callback_data)
    assert "Страница 2/2" in session.sent[-1].text
    old = core.sessions(conv)[-1]
    await click(
        channel, 3, session.sent[-1], button(session.sent[-1], f"#{old.id} ·").callback_data
    )
    assert core.active_session(conv).id != old.id
    preview = session.sent[-1]
    await click(channel, 4, preview, button(preview, "Выбрать диалог").callback_data)
    assert core.active_session(conv).id == old.id
    channel._recent.clear()
    await click(channel, 5, session.sent[-1], button(session.sent[-1], "← Диалоги").callback_data)
    assert "Страница 2/2" in session.sent[-1].text
    assert not server.log


async def test_owned_job_result_button_is_independent_of_current_route(telegram, tmp_path):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "saved", "/work")
    job = core.store.add_job("saved-job", record.id)
    core.store.finish_job(job.id, JobStatus.COMPLETED, answer="**saved** `code`")
    await channel._send_answer(OWNER, "**saved** `code`", core.job(job.id))
    answer = session.sent[-1]
    assert "<b>saved</b>" in answer.text and "<code>code</code>" in answer.text
    assert "work" in button(answer, "🖥").text
    from pydantic import SecretStr
    from test_telegram import SECRET

    from acp_gateway.agents import AgentClient

    core.add_agent(
        AgentClient(
            core.agent("work").profile.model_copy(update={"alias": "other"}),
            SecretStr(SECRET),
            pin_dir=tmp_path / "other-pins",
        )
    )
    await feed(channel, 1, "/agent other")
    await click(channel, 2, answer, button(answer, "Результат").callback_data)
    await eventually(lambda: len([m for m in session.sent if m.text == answer.text]) == 2)
    await click(channel, 3, answer, button(answer, "Результат").callback_data, user=OTHER)
    assert len([m for m in session.sent if m.text == answer.text]) == 2


async def test_job_cancel_button_targets_exact_owned_job_and_is_one_shot(
    telegram, monkeypatch, tmp_path
):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "saved", "/work")
    job = core.store.add_job("running-job", record.id)
    calls = []

    async def cancel(job_id):
        calls.append(job_id)
        core.store.finish_job(job_id, JobStatus.CANCELLED)
        return core.job(job_id)

    monkeypatch.setattr(core, "cancel_job", cancel)
    await channel._panel(OWNER, "Working", channel._job_rows(job))
    message = session.sent[-1]
    from pydantic import SecretStr
    from test_telegram import SECRET

    from acp_gateway.agents import AgentClient

    core.add_agent(
        AgentClient(
            core.agent("work").profile.model_copy(update={"alias": "other"}),
            SecretStr(SECRET),
            pin_dir=tmp_path / "other-pins",
        )
    )
    await feed(channel, 1, "/agent other")
    data = button(message, "Остановить").callback_data
    await click(channel, 2, message, data)
    await click(channel, 3, message, data)
    assert calls == [job.id]


async def test_legacy_bound_delivery_keeps_its_original_chunks(telegram):
    import hashlib
    import json

    from test_telegram_delivery import pause_sender, saved

    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    job = saved(core, channel, "**" + "A" * 2500 + "**")
    chunks = channel._chunks(core.job(job.id).answer)
    digest = hashlib.sha256(json.dumps(chunks, ensure_ascii=True).encode()).hexdigest()
    core.store._conn.execute(
        "UPDATE outbound_deliveries SET content_hash=?,next_chunk=1 WHERE job_id=?",
        (digest, job.id),
    )
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    assert session.sent[-1].text == chunks[1]
    assert core.store.deliveries.get(channel.namespace, job.id)["state"] == "delivered"


async def test_parse_rejection_falls_back_but_network_error_never_retries(telegram):
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

    _, channel, session, _ = telegram
    session.failures.append(
        lambda method: TelegramBadRequest(method=method, message="can't parse entities")
    )
    await channel._send_answer(OWNER, "**bold** `x < y`")
    assert session.sent[-1].text == "bold x < y"
    call = next(c for c in reversed(session.calls) if isinstance(c, SendMessage))
    assert call.parse_mode is None and call.link_preview_options.is_disabled
    before = len(session.calls)
    session.failures.append(lambda method: TelegramNetworkError(method=method, message="ambiguous"))
    with pytest.raises(TelegramNetworkError):
        await channel._send_answer(OWNER, "**one attempt**")
    assert len(session.calls) == before + 1


async def test_model_catalogue_selection_confirmation_and_stale_selection(telegram, monkeypatch):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    active = core.active_session(conv)
    revision = core.store.active_revision(conv)
    result = {
        "dialogue_id": active.id,
        "selection_revision": revision,
        "provider": "provider-a",
        "model": "model-a",
        "pending": None,
        "providers": [{"provider": "provider-a", "models": ["model-a", "model-b"]}],
    }
    calls = []
    confirmation = "model-confirmation-value-123456"

    async def options(*args, **kwargs):
        return result

    async def select(*args, **kwargs):
        calls.append(kwargs)
        return {
            "dialogue_id": active.id,
            "selection_revision": revision,
            "status": "accepted" if kwargs.get("confirmation_token") else "confirmation_required",
            "confirmation_token": confirmation,
            "message": "Confirm switch",
        }

    monkeypatch.setattr(core, "model_options", options)
    monkeypatch.setattr(core, "set_model", select)
    await feed(channel, 2, "/models")
    catalogue = session.sent[-1]
    await click(channel, 3, catalogue, button(catalogue, "model-b").callback_data)
    confirm = session.sent[-1]
    assert confirmation not in confirm.text
    assert "provider-a / model-b" in confirm.text
    data = button(confirm, "Подтвердить").callback_data
    assert confirmation not in data
    await click(channel, 4, confirm, data)
    assert calls[-1]["confirmation_token"] == confirmation
    await click(channel, 5, confirm, data)
    assert len(calls) == 2
    channel._recent.clear()
    await feed(channel, 6, "/models")
    stale = session.sent[-1]
    await feed(channel, 7, "/new")
    await click(channel, 8, stale, button(stale, "model-b").callback_data)
    assert len(calls) == 2


async def test_new_dialogue_does_not_overwrite_agent_selected_during_rpc(telegram, monkeypatch):
    core, channel, session, _ = telegram
    create = core.new_session

    async def new(conv):
        record = await create(conv)
        channel._set_route(OWNER, "work")
        return record

    monkeypatch.setattr(core, "new_session", new)
    await feed(channel, 1, "/new")
    assert "Selected agent changed" in session.sent[-1].text
    assert channel._ui_context(OWNER)[1] == 1


async def test_concurrent_new_dialogue_clicks_submit_once(telegram, monkeypatch):
    import asyncio

    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    message = session.sent[-1]
    data = button(message, "Новый диалог").callback_data
    entered, release = asyncio.Event(), asyncio.Event()
    create = core.new_session
    calls = []

    async def paused(conv):
        calls.append(conv)
        entered.set()
        await release.wait()
        return await create(conv)

    monkeypatch.setattr(core, "new_session", paused)
    first = asyncio.create_task(click(channel, 2, message, data))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await click(channel, 3, message, data)
        assert len(calls) == 1
    finally:
        release.set()
        await first
    assert len(core.sessions(Conversation("telegram", str(OWNER), "work"))) == 1


async def test_cancel_control_is_not_shown_when_agent_refuses_safe_cancel(telegram, monkeypatch):
    core, channel, _, _ = telegram
    from types import SimpleNamespace

    monkeypatch.setattr(core, "agent", lambda alias: SimpleNamespace(supports_cancel=False))
    job = SimpleNamespace(
        conversation=Conversation("telegram", str(OWNER), "work"),
        session_id=1,
        id="job",
        status=JobStatus.RUNNING,
    )
    assert not any(
        command.startswith("cancel:") for row in channel._job_rows(job) for _, command in row
    )


async def test_execution_mode_buttons_authorize_only_selected_dialogue_and_restore_ask(telegram):
    from test_telegram import approval_message

    from acp_gateway.core import GatewayError

    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    first = core.active_session(conv)
    await feed(channel, 3, "/mode")
    panel = session.sent[-1]
    auto = button(panel, "Без подтверждений").callback_data
    await click(channel, 4, panel, auto, user=OTHER)
    assert core.execution_mode(conv)["mode"] == "ask"
    await click(channel, 5, panel, auto)
    assert core.execution_mode(conv)["mode"] == "auto"
    channel._recent.clear()
    await feed(channel, 6, "run exactly: echo hi .")
    job = core.running_jobs()[0]
    final = await core.wait(job.id, 5)
    assert final.answer == "hi" and final.error is None
    (audit,) = core.store.approval_audit(job.id)
    assert audit.actor == f"telegram:{OWNER}" and "auto" in audit.reason
    assert not any(approval_message(m) for m in session.sent)
    # The setting is durable but does not spread to a new dialogue or another chat.
    await feed(channel, 7, "/new")
    second = core.active_session(conv)
    assert second.id != first.id and core.execution_mode(conv)["mode"] == "ask"
    await click(channel, 8, panel, auto)  # Used/stale control cannot authorize the new one.
    assert core.execution_mode(conv)["mode"] == "ask"
    with pytest.raises(GatewayError):
        core.set_execution_mode(
            conv,
            "auto",
            dialogue_id=first.id,
            expected_revision=core.store.active_revision(conv),
            actor=f"telegram:{OWNER}",
        )
    channel._recent.clear()
    await feed(channel, 9, f"/switch {first.id}")
    assert core.execution_mode(conv)["mode"] == "auto"
    await feed(channel, 10, "/mode")
    panel = session.sent[-1]
    await click(channel, 11, panel, button(panel, "Как Hermes").callback_data)
    assert core.execution_mode(conv)["mode"] == "ask"
    await feed(channel, 12, "run exactly: echo hi .")
    job = core.running_jobs()[0]
    await eventually(lambda: any(approval_message(m) for m in session.sent))
    core.approvals.cancel_job(job.id)
    assert (await core.wait(job.id, 5)).answer == "DENIED"


async def test_execution_authorization_rejects_busy_foreign_actor_and_disabled_policy(telegram):
    from acp_gateway.core import GatewayError

    core, channel, _, _ = telegram
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    state = core.execution_mode(conv)

    def change(actor=f"telegram:{OWNER}"):
        return core.set_execution_mode(
            conv,
            "auto",
            dialogue_id=state["dialogue_id"],
            expected_revision=state["selection_revision"],
            actor=actor,
        )

    with pytest.raises(GatewayError):
        change(f"telegram:{OTHER}")
    from acp_gateway.agents import SessionBusy

    core._busy[state["dialogue_id"]] = "busy-test"
    with pytest.raises(SessionBusy):
        change()
    core._busy.clear()
    core.policy.settings.allow_approvals = False
    with pytest.raises(GatewayError):
        change()
    assert core.execution_mode(conv)["mode"] == "ask"


async def test_reused_storage_id_cannot_inherit_another_dialogues_authorization(telegram):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    first = core.active_session(conv)
    core.set_execution_mode(
        conv,
        "auto",
        dialogue_id=first.id,
        expected_revision=core.store.active_revision(conv),
        actor=f"telegram:{OWNER}",
    )
    core.store._conn.execute(
        "UPDATE sessions SET acp_session_id = 'replacement' WHERE id = ?", (first.id,)
    )
    assert core.execution_mode(conv)["mode"] == "ask"


@pytest.mark.parametrize(
    "field,value",
    [
        ("session", "other-session"),
        ("agent", "other-route"),
        ("actor", "telegram:999"),
        ("channel", "mcp"),
    ],
)
async def test_mode_display_never_claims_another_scopes_authorization(telegram, field, value):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    state = core.execution_mode(conv)
    core.set_execution_mode(
        conv,
        "auto",
        dialogue_id=state["dialogue_id"],
        expected_revision=state["selection_revision"],
        actor=f"telegram:{OWNER}",
    )
    saved = core.store.channel_state("execution_mode", str(state["dialogue_id"]))
    saved[field] = value
    core.store.set_channel_state("execution_mode", str(state["dialogue_id"]), saved)
    assert core.execution_mode(conv)["mode"] == "ask"


async def test_dialogue_preview_is_private_read_only_and_keeps_the_page(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    records = [core.store.add_session(conv, f"saved-{i}", "/work") for i in range(12)]
    old = records[0]
    core.store.set_session_title(old.id, "Research <draft>")
    revision = core.store.active_revision(conv)
    active = core.active_session(conv).id
    await feed(channel, 1, "/sessions 2")
    page = session.sent[-1]
    stale = button(page, f"#{old.id} ·").callback_data
    await click(channel, 2, page, stale)
    preview = session.sent[-1]
    assert "Research &lt;draft&gt;" in preview.text
    assert (
        "Агент / ПК: work" in plain_html(preview.text) and "Последняя активность:" in preview.text
    )
    assert "Не выбран" in preview.text
    assert core.active_session(conv).id == active
    assert core.store.active_revision(conv) == revision
    await click(channel, 3, preview, button(preview, "← Диалоги").callback_data)
    assert "Страница 2/2" in session.sent[-1].text
    await click(channel, 4, page, stale)
    assert "Страница 2/2" in session.sent[-1].text
    assert any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in session.calls)
    assert core.store.active_revision(conv) == revision
    assert not server.log


async def test_dialogue_preview_and_selection_reject_foreign_scope(telegram):
    from acp_gateway.core import GatewayError

    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    own = core.store.add_session(conv, "own", "/work")
    for foreign_conv in [
        Conversation("telegram", str(OTHER), "work"),
        Conversation("cli", str(OWNER), "work"),
        Conversation("telegram", str(OWNER), "other"),
    ]:
        foreign = core.store.add_session(
            foreign_conv,
            f"foreign-{foreign_conv.agent}-{foreign_conv.channel}-{foreign_conv.key}",
            "/private",
        )
        core.store.set_session_title(foreign.id, "PRIVATE DIALOGUE")
        for command in ["/dialogue", "/switch"]:
            with pytest.raises(GatewayError, match="does not own"):
                await channel._command(OWNER, f"{command} {foreign.id}")
    assert core.active_session(conv).id == own.id
    assert not session.sent
    assert not server.log


@pytest.mark.parametrize("argument", ["1 0", "1 999", "1 -2", "1 1 extra", "\u0661", "9" * 11])
async def test_invalid_dialogue_navigation_does_not_change_selection(telegram, argument):
    from acp_gateway.core import GatewayError

    core, channel, _, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    core.store.add_session(conv, "own", "/work")
    revision = core.store.active_revision(conv)
    for command in ["/dialogue", "/switch"]:
        with pytest.raises(GatewayError):
            await channel._command(OWNER, f"{command} {argument}")
    assert core.store.active_revision(conv) == revision
    assert not server.log


async def test_dialogue_selection_button_rejects_changed_active_dialogue(telegram):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    first = core.store.add_session(conv, "first", "/work")
    second = core.store.add_session(conv, "second", "/work")
    await channel._command(OWNER, f"/dialogue {first.id}")
    preview = session.sent[-1]
    data = button(preview, "Выбрать диалог").callback_data
    core.store.set_active(conv, first.id)
    core.store.set_active(conv, second.id)
    await click(channel, 1, preview, data)
    assert core.active_session(conv).id == second.id
    assert any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in session.calls)
