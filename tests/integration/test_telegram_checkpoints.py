# ruff: noqa: RUF001
"""One button asks the agent to wrap up and keeps its return point beyond job retention."""

import asyncio
from datetime import timedelta

from test_telegram import OWNER, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.core.gateway import CHECKPOINT_PROMPT
from acp_gateway.storage import Conversation
from acp_gateway.storage.records import utcnow


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def prompts(core):
    rows = core.store._conn.execute(
        "SELECT p.prompt FROM jobs j JOIN job_prompts p ON p.job_id = j.id"
        " ORDER BY j.created_at, j.rowid"
    ).fetchall()
    return [row[0] for row in rows]


async def card(channel, session, dialogue_id):
    await channel._command(OWNER, f"/dialogue {dialogue_id}")
    return session.sent[-1]


async def test_idle_dialogue_saves_return_point_and_continues_from_it(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.active_session(conv)
    panel = await card(channel, session, record.id)
    await click(channel, 2, panel, button(panel, "Завершить и сохранить").callback_data)
    await eventually(lambda: core.checkpoint_of(record.id) is not None)
    saved = core.checkpoint_of(record.id)
    assert saved["text"].startswith("echo: Аккуратно заверши текущую работу")
    assert prompts(core) == [CHECKPOINT_PROMPT]
    # The return point outlives the seven-day job retention.
    core.store.prune_jobs(utcnow() + timedelta(days=30))
    assert core.checkpoint_of(record.id) == saved
    panel = await card(channel, session, record.id)
    assert "📌 Точка возврата" in panel.text
    await click(channel, 3, panel, button(panel, "Точка возврата").callback_data)
    shown = session.sent[-1]
    assert "echo: Аккуратно" in shown.text
    await click(channel, 4, shown, button(shown, "Продолжить в этом диалоге").callback_data)
    await eventually(lambda: not core.running_jobs())
    assert prompts(core)[-1].startswith("Продолжи работу с сохранённой точки возврата")
    assert saved["text"] in prompts(core)[-1]
    assert core.active_session(conv).id == record.id


async def test_continue_in_new_dialogue_keeps_the_original(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.active_session(conv)
    result = await core.checkpoint(conv, record.id)
    await core.wait(result["job"].id, 5)
    shown_before = len(session.sent)
    await channel._command(OWNER, f"/checkpoint_show {record.id}")
    shown = session.sent[-1]
    assert len(session.sent) > shown_before
    await click(channel, 2, shown, button(shown, "Продолжить в новом диалоге").callback_data)
    await eventually(lambda: not core.running_jobs())
    sessions = core.store._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert sessions == 2
    assert core.checkpoint_of(record.id) is not None


async def busy_dialogue(core, channel, session):
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.active_session(conv)
    # Goose waits for approval of this command, so the dialogue stays busy.
    await feed(channel, 2, "run exactly: sleep 30 .")
    await eventually(lambda: bool(core.running_jobs()))
    await eventually(lambda: bool(core.pending_approvals("telegram")))
    running = core.running_jobs()[0]
    panel = await card(channel, session, record.id)
    await click(channel, 3, panel, button(panel, "Завершить и сохранить").callback_data)
    choice = session.sent[-1]
    assert "сейчас выполняет задачу" in choice.text
    return conv, record, running, choice


async def test_busy_dialogue_writes_return_point_after_the_current_job(telegram):
    core, channel, session, _ = telegram
    _, record, running, choice = await busy_dialogue(core, channel, session)
    await click(channel, 4, choice, button(choice, "После текущей задачи").callback_data)
    assert "будет запрошена" in session.sent[-1].text
    assert core.pending_checkpoint(record.id) and core.checkpoint_of(record.id) is None
    assert "после текущей задачи" in (await card(channel, session, record.id)).text
    core.approvals.cancel_job(running.id)  # the running job ends on its own
    await eventually(lambda: core.checkpoint_of(record.id) is not None)
    assert (await core.wait(running.id, 5)).answer == "DENIED"
    assert prompts(core) == ["run exactly: sleep 30 .", CHECKPOINT_PROMPT]


async def test_interrupt_and_save_stops_the_job_first(telegram):
    core, channel, session, _ = telegram
    _, record, running, choice = await busy_dialogue(core, channel, session)
    await click(channel, 4, choice, button(choice, "Прервать и сохранить").callback_data)
    await eventually(lambda: core.checkpoint_of(record.id) is not None)
    assert (await core.wait(running.id, 5)).status.value == "cancelled"
    assert prompts(core)[-1] == CHECKPOINT_PROMPT


async def test_scheduled_return_point_can_be_cancelled(telegram):
    core, channel, session, _ = telegram
    _, record, running, choice = await busy_dialogue(core, channel, session)
    await click(channel, 4, choice, button(choice, "После текущей задачи").callback_data)
    scheduled = session.sent[-1]
    await click(channel, 5, scheduled, button(scheduled, "Отменить").callback_data)
    assert not core.pending_checkpoint(record.id)
    core.approvals.cancel_job(running.id)
    assert (await core.wait(running.id, 5)).status.finished
    await asyncio.sleep(0.05)
    assert core.checkpoint_of(record.id) is None
    assert prompts(core) == ["run exactly: sleep 30 ."]
