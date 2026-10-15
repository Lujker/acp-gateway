"""Dispatcher names, complete approval input and factual work status."""

import asyncio
import html
import json

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from test_telegram import OTHER, OWNER, approval_message, click, eventually, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.agents.client import PermissionOption, PermissionRequest
from acp_gateway.core import GatewayCore, GatewayError
from acp_gateway.storage import Conversation, Store


async def requested(telegram, raw):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "approval-test", "/work")
    job = core.store.add_job("approval-job", record.id)
    request = PermissionRequest(
        record.acp_session_id,
        "tool",
        "Run command",
        "execute",
        raw,
        (
            PermissionOption("allow", "Allow", "allow_once"),
            PermissionOption("deny", "Deny", "reject_once"),
        ),
    )
    task = asyncio.create_task(core.approvals.request(conv, job.id, request))
    await eventually(lambda: bool(channel._buttons))
    approval = core.pending_approvals(channel.name)[0]
    return job, approval, task, next(m for m in session.sent if approval_message(m))


async def test_full_approval_input_pages_are_complete_private_and_settled_once(telegram):
    core, channel, session, _ = telegram
    raw = {"command": "echo " + "<part>" * 700 + " END-OF-COMMAND"}
    job, approval, task, compact = await requested(telegram, raw)
    assert "Ввод сокращён" in compact.text
    assert f"Диалог: #{job.session_id}" in compact.text
    with pytest.raises(GatewayError):
        await channel._command(OTHER, f"/approval {approval.id}")
    encoded = json.dumps(raw, ensure_ascii=False, indent=2)
    parts = []
    for page in range(1, (len(encoded) + 999) // 1000 + 1):
        await channel._command(OWNER, f"/approval {approval.id} {page}")
        card = session.sent[-1]
        assert len(html.unescape(card.text)) < 2000
        parts.append(html.unescape(card.text).split(f"страница {page}/", 1)[1].split("\n", 1)[1])
    assert "".join(parts) == encoded
    assert core.pending_approvals(channel.name)[0].id == approval.id
    await click(channel, 2, compact, compact.reply_markup.inline_keyboard[0][1].callback_data)
    assert (await task).outcome == "rejected"
    await eventually(lambda: not channel._buttons)
    assert all(not m.reply_markup for m in session.sent if "Подтверждение ·" in m.text)
    assert all("Итог: ❌ Отклонено" in m.text for m in session.sent if "Подтверждение ·" in m.text)
    assert len(core.store.approval_audit(job.id)) == 1
    with pytest.raises(GatewayError):
        await channel._command(OWNER, f"/approval {approval.id}")


async def test_approval_send_racing_resolution_cannot_leave_live_controls(telegram):
    _, channel, session, _ = telegram
    _, approval, task, compact = await requested(telegram, {"command": "echo test"})
    entered, release = asyncio.Event(), asyncio.Event()
    original = channel._send

    async def delayed(chat, text, **kwargs):
        if "Полный ввод ·" in text:
            entered.set()
            await release.wait()
        return await original(chat, text, **kwargs)

    channel._send = delayed
    details = asyncio.create_task(channel._command(OWNER, f"/approval {approval.id}"))
    await entered.wait()
    await click(channel, 2, compact, compact.reply_markup.inline_keyboard[0][0].callback_data)
    assert (await task).outcome == "approved"
    await eventually(lambda: not channel._buttons)
    release.set()
    await details
    assert not channel._buttons and not channel._approval_texts
    assert "Итог: ✅ Разрешено один раз" in session.sent[-1].text
    assert not session.sent[-1].reply_markup


async def test_dispatcher_rename_is_scoped_persistent_and_preserves_host_title(telegram, tmp_path):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "rename-test", "/work")
    revision = core.store.active_revision(conv)
    await channel._command(OWNER, f"/rename {record.id} Research <draft>")
    assert "Research &lt;draft&gt;" in session.sent[-1].text
    assert "Hermes не менялось" in session.sent[-1].text
    core.store.set_session_title(record.id, "HOST TITLE")
    assert core.active_session(conv).title == "Research <draft>"
    assert core.store.session(record.id).title == "HOST TITLE"
    assert core.store.active_revision(conv) == revision
    reopened = Store.open(tmp_path / "gateway.db")
    try:
        reloaded = GatewayCore(reopened, core.agents, policy=core.policy.settings)
        assert reloaded.sessions(conv)[0].title == "Research <draft>"
    finally:
        reopened.close()
    foreign = core.store.add_session(Conversation("telegram", str(OTHER), "work"), "foreign", "/x")
    with pytest.raises(GatewayError, match="does not own"):
        await channel._command(OWNER, f"/rename {foreign.id} PRIVATE")
    for title in ["", "x" * 121, "bad\x00title"]:
        with pytest.raises(GatewayError):
            core.rename_dialogue(conv, record.id, title)
    assert not server.log


async def test_progress_shows_approval_wait_route_duration_and_actual_finish(telegram):
    _, channel, session, _ = telegram
    channel.settings.status_interval_seconds = 1
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    await feed(channel, 3, "run exactly: echo hi .")
    await eventually(lambda: bool(channel._buttons))
    await eventually(lambda: any("Ожидает подтверждения" in m.text for m in session.sent))
    progress = next(m for m in session.sent if "Ожидает подтверждения" in m.text)
    assert (
        "work" in progress.text
        and "Длительность:" in progress.text
        and "Диалог: #" in progress.text
    )
    compact = next(m for m in session.sent if approval_message(m))
    await click(channel, 4, compact, compact.reply_markup.inline_keyboard[0][0].callback_data)
    await eventually(lambda: not channel._statuses)
    final = next(m for m in session.sent if m.message_id == progress.message_id)
    assert "Завершена" in final.text and "/result" in final.text
    assert "Ожидает подтверждения" not in final.text
    assert any(m.text == "hi" for m in session.sent)
    assert any(isinstance(c, EditMessageText) for c in session.calls)
    assert not any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in session.calls)


async def test_job_navigation_preserves_active_filter_cursor_and_delivery_return(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "navigation-test", "/work")
    for i in range(12):
        core.store.add_job(f"navigation-{i}", record.id)
    history = core.job_history("telegram", str(OWNER), active=True)
    cursor = history["next_before"]
    assert cursor
    await feed(channel, 1, f"/jobs active before {cursor}")
    page = session.sent[-1]
    old_text = page.text
    target = core.job_history("telegram", str(OWNER), active=True, before=cursor)["jobs"][0]
    await click(channel, 2, page, button(page, target.id[:8]).callback_data)
    assert "Задача " in session.sent[-1].text
    await click(channel, 3, session.sent[-1], button(session.sent[-1], "Доставка").callback_data)
    await click(channel, 4, session.sent[-1], button(session.sent[-1], "← Задача").callback_data)
    await click(channel, 5, session.sent[-1], button(session.sent[-1], "← Задачи").callback_data)
    assert session.sent[-1].text == old_text
    assert not server.log


async def test_job_dialogue_selection_targets_exact_owned_route_and_is_once_only(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    old = core.store.add_session(conv, "old-dialogue", "/work")
    job = core.store.add_job("old-dialogue-job", old.id)
    core.store.add_session(conv, "current-dialogue", "/work")
    await feed(channel, 1, f"/job {job.id}")
    card = session.sent[-1]
    data = button(card, "Выбрать диалог").callback_data
    await click(channel, 2, card, data)
    assert core.active_session(conv).id == old.id
    assert "Агент / ПК: work" in session.sent[-1].text
    revision = core.store.active_revision(conv)
    await click(channel, 3, card, data)
    assert core.store.active_revision(conv) == revision
    assert session.calls[-1].show_alert
    foreign = core.store.add_session(
        Conversation("telegram", str(OTHER), "work"), "foreign-job", "/x"
    )
    other_job = core.store.add_job("foreign-job", foreign.id)
    with pytest.raises(GatewayError):
        await channel._command(OWNER, f"/open_dialogue {other_job.id}")
    assert core.active_session(conv).id == old.id
    assert not server.log
