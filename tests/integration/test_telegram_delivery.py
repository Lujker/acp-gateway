"""Restart-safe answer delivery and independent Telegram status edits."""

import asyncio
import secrets

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import EditMessageText, SendMessage
from test_telegram import OWNER, eventually, feed
from test_telegram import telegram as telegram

from acp_gateway.storage import Conversation, JobStatus


async def pause_sender(channel):
    task = next(t for t in channel._tasks if t.get_name() == "telegram-outbound")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    channel._tasks.remove(task)


def saved(core, channel, answer, *, chat=OWNER, name="saved"):
    session = core.store.add_session(Conversation("telegram", str(chat), "work"), name, "/work")
    job = core.store.add_job(name, session.id, delivery_namespace=channel.namespace)
    core.store.finish_job(job.id, JobStatus.COMPLETED, answer=answer)
    return job


async def test_acknowledged_prefix_is_not_sent_again(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    job = saved(core, channel, "A" * 2000 + "B" * 2000 + "C")
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    assert core.store.deliveries.get(channel.namespace, job.id)["next_chunk"] == 1
    core.store.deliveries.recover(channel.namespace)
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    assert [m.text for m in session.sent if m.text in {"A" * 2000, "B" * 2000, "C"}] == [
        "A" * 2000,
        "B" * 2000,
        "C",
    ]
    assert core.store.deliveries.get(channel.namespace, job.id)["state"] == "delivered"


@pytest.mark.parametrize(
    "failure,state",
    [
        ("network", "unknown"),
        ("rejected", "pending"),
        ("bad_request", "failed"),
    ],
)
async def test_send_failures_never_blindly_replay(telegram, failure, state):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    job = saved(core, channel, "private saved answer")
    exceptions = {
        "network": lambda m: TelegramNetworkError(method=m, message="timeout"),
        "rejected": lambda m: TelegramRetryAfter(method=m, message="slow", retry_after=31),
        "bad_request": lambda m: TelegramBadRequest(method=m, message="invalid"),
    }
    session.failures.append(exceptions[failure])
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    assert core.store.deliveries.get(channel.namespace, job.id)["state"] == state
    assert core.store.deliveries.due(channel.namespace) is None
    await feed(channel, 2, f"/delivery {job.id}")
    assert state in session.sent[-1].text
    await feed(channel, 3, f"/result {job.id}")
    await eventually(lambda: any(m.text == "private saved answer" for m in session.sent))


async def test_lost_local_ack_does_not_repeat_remote_send(telegram, monkeypatch):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    job = saved(core, channel, "already sent")

    def failed(*args, **kwargs):
        raise OSError("disk full after Telegram accepted")

    monkeypatch.setattr(core.store.deliveries, "acknowledge", failed)
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    core.store.deliveries.recover(channel.namespace)
    assert core.store.deliveries.get(channel.namespace, job.id)["state"] == "unknown"
    assert core.store.deliveries.due(channel.namespace) is None
    assert sum(m.text == "already sent" for m in session.sent) == 1


async def test_removed_recipient_is_not_sent_saved_content(telegram):
    core, channel, session, _ = telegram
    await pause_sender(channel)
    job = saved(core, channel, "do not send", chat=99)
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    assert not session.sent
    assert core.store.deliveries.get(channel.namespace, job.id)["state"] == "failed"


async def test_working_message_is_edited_to_final_status(telegram):
    _, channel, session, _ = telegram
    channel.settings.status_interval_seconds = 1
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    await feed(channel, 3, "say pong")
    await eventually(lambda: any(isinstance(c, EditMessageText) for c in session.calls))
    edit = next(c for c in session.calls if isinstance(c, EditMessageText))
    sends = [c for c in session.calls if isinstance(c, SendMessage)]
    working_id = next(i + 1 for i, c in enumerate(sends) if c.text.startswith("Working:"))
    assert edit.message_id == working_id and edit.chat_id == OWNER
    assert next(m for m in session.sent if m.message_id == working_id).text == edit.text
    assert "completed" in edit.text and "/result" in edit.text
    assert any(m.text == "pong" for m in session.sent)
    assert not channel._statuses


async def test_blocked_status_edit_does_not_block_delivery_or_cancel(telegram):
    _, channel, session, _ = telegram
    channel.settings.status_interval_seconds = 1
    blocked = asyncio.Event()
    release = asyncio.Event()
    original = session.make_request

    async def slow(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            blocked.set()
            await release.wait()
        return await original(bot, method, timeout)

    session.make_request = slow
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    await feed(channel, 3, "say pong")
    try:
        await asyncio.wait_for(blocked.wait(), 3)
        assert any(m.text == "pong" for m in session.sent)
        await feed(channel, 4, "/stop")
        assert any(m.text.startswith("Stopped jobs:") for m in session.sent)
    finally:
        release.set()


@pytest.mark.parametrize("inflight", [False, True])
async def test_restart_delivers_pending_but_fences_vanished_sender(telegram, tmp_path, inflight):
    from aiogram import Bot
    from pydantic import SecretStr
    from test_telegram import TOKEN

    from acp_gateway.channels.telegram import TelegramChannel
    from acp_gateway.core import GatewayCore
    from acp_gateway.storage import Store
    from fakes.telegram import FakeTelegramSession

    core, channel, _, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    saved(core, channel, "survived restart")
    if inflight:
        assert core.store.deliveries.claim(channel.namespace, "saved")
    await core.close()
    reopened = GatewayCore(Store.open(tmp_path / "gateway.db"), {})
    fresh_session = FakeTelegramSession()
    fresh_channel = TelegramChannel(
        channel.settings, SecretStr(TOKEN), bot=Bot(TOKEN, session=fresh_session)
    )
    reopened.add_channel(fresh_channel)
    try:
        await reopened.start()
        if inflight:
            assert reopened.store.deliveries.get(channel.namespace, "saved")["state"] == "unknown"
            await asyncio.sleep(0.05)
            assert not fresh_session.sent
        else:
            await eventually(lambda: bool(fresh_session.sent))
            assert fresh_session.sent[0].text == "survived restart"
            assert reopened.store.deliveries.get(channel.namespace, "saved")["state"] == "delivered"
    finally:
        await reopened.close()


async def test_changed_rendering_fences_remaining_chunks(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    saved(core, channel, "A" * 2000 + "B")
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    core.store._conn.execute("UPDATE jobs SET answer = ? WHERE id = 'saved'", ("changed",))
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    assert core.store.deliveries.get(channel.namespace, "saved")["state"] == "unknown"
    assert not any(m.text == "changed" for m in session.sent)


async def test_status_failure_does_not_hide_saved_final_answer(telegram):
    _, channel, session, _ = telegram
    channel.settings.status_interval_seconds = 1
    original = session.make_request

    async def broken(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramNetworkError(method=method, message="lost status edit")
        return await original(bot, method, timeout)

    session.make_request = broken
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    await feed(channel, 3, "say pong")
    await eventually(lambda: any(m.text == "pong" for m in session.sent))
    await eventually(lambda: not channel._statuses)


async def test_partial_status_redacts_before_truncation_and_escapes_html(telegram):
    from acp_gateway.log import register_secret

    core, channel, session, _ = telegram
    channel.settings.status_interval_seconds = 1
    await feed(channel, 1, "/start")
    record = core.store.add_session(
        Conversation("telegram", str(OWNER), "work"), "partial", "/work"
    )
    job = core.store.add_job("partial-job", record.id)
    working = await channel._send(OWNER, "Working: partial-job")
    secret = secrets.token_urlsafe(24)
    register_secret(secret)
    core.store._conn.execute(
        "UPDATE jobs SET answer = ? WHERE id = ?", ("<hello>" + secret + "x" * 3000, job.id)
    )
    channel._statuses[job.id] = (OWNER, working.message_id, "Working: partial-job", 0)
    await eventually(lambda: any(isinstance(c, EditMessageText) for c in session.calls))
    edit = next(c for c in session.calls if isinstance(c, EditMessageText))
    assert "&lt;hello&gt;" in edit.text and secret not in edit.text
    assert "\\n" not in edit.text and "\n" in edit.text
    assert len(edit.text) < 2000
    core.store.finish_job(job.id, JobStatus.CANCELLED)
    await eventually(lambda: not channel._statuses)
    assert any("cancelled" in c.text for c in session.calls if isinstance(c, EditMessageText))


async def test_status_429_defers_edit_without_duplicate_message(telegram):
    _, channel, session, _ = telegram
    channel.settings.status_interval_seconds = 1
    original = session.make_request
    edits = []

    async def reject_once(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            edits.append(method)
            if len(edits) == 1:
                raise TelegramRetryAfter(method=method, message="slow edit", retry_after=1)
        return await original(bot, method, timeout)

    session.make_request = reject_once
    await feed(channel, 1, "/start")
    await feed(channel, 2, "/new")
    await feed(channel, 3, "say pong")
    await eventually(lambda: len(edits) >= 2)
    assert len({edit.message_id for edit in edits}) == 1
    assert (
        sum(isinstance(c, SendMessage) and c.text.startswith("Working:") for c in session.calls)
        == 1
    )
    assert sum(m.message_id == edits[0].message_id for m in session.sent) == 1
