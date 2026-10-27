"""An interrupted dialogue is continued only by an explicit button; nothing is replayed."""

import pytest
from test_telegram import OWNER, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.agents import SessionInterrupted
from acp_gateway.storage import Conversation


def jobs(core):
    return core.store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


async def test_message_to_interrupted_dialogue_offers_explicit_resume(telegram, monkeypatch):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "native-1", "/work")
    agent = core.agent("work")
    state = {"native-1": "interrupted"}
    monkeypatch.setattr(agent, "session_availability", lambda sid: state.get(sid, "attached"))
    resumed = []

    async def resume(sid, cwd=None):
        resumed.append((sid, cwd))
        state[sid] = "attached"

    monkeypatch.setattr(agent, "resume_interrupted", resume, raising=False)

    await feed(channel, 1, "are you there?")
    panel = session.sent[-1]
    assert "прерван" in panel.text and "не отправлено агенту" in panel.text
    assert jobs(core) == 0 and not server.log
    data = button(panel, "Возобновить диалог").callback_data
    await click(channel, 2, panel, data)
    assert resumed == [("native-1", "/work")]
    assert "продолжен" in session.sent[-1].text
    assert "не отправлялся повторно" in session.sent[-1].text
    # The button works once; the interrupted prompt is never replayed.
    await click(channel, 3, panel, data)
    assert resumed == [("native-1", "/work")]
    assert jobs(core) == 0 and not server.log
    assert core.active_session(conv).id == record.id


async def test_core_refuses_job_for_interrupted_dialogue(telegram, monkeypatch):
    core, _, _, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    core.store.add_session(conv, "native-1", "/work")
    monkeypatch.setattr(core.agent("work"), "session_availability", lambda sid: "interrupted")
    with pytest.raises(SessionInterrupted):
        await core.submit_selected(conv, "hello")
    assert jobs(core) == 0 and not server.log


async def test_dialogue_card_shows_interrupted_state_and_resume(telegram, monkeypatch):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "native-1", "/work")
    monkeypatch.setattr(core.agent("work"), "session_availability", lambda sid: "interrupted")
    await channel._command(OWNER, f"/dialogue {record.id}")
    card = session.sent[-1]
    assert "предыдущий ход прерван" in card.text
    assert button(card, "Возобновить диалог")


async def test_fenced_dialogue_is_released_only_after_explicit_confirmation(telegram, monkeypatch):
    from acp_gateway.agents import SessionFenced

    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "native-1", "/work")
    agent = core.agent("work")
    state = {"native-1": "fenced"}
    monkeypatch.setattr(agent, "session_availability", lambda sid: state.get(sid, "attached"))
    released = []

    async def release(sid, cwd=None):
        released.append((sid, cwd))
        state[sid] = "attached"

    monkeypatch.setattr(agent, "release_fenced", release, raising=False)
    with pytest.raises(SessionFenced):
        await core.submit_selected(conv, "hello")
    await feed(channel, 1, "are you there?")
    panel = session.sent[-1]
    assert "заблокирован" in panel.text and "не отправлено агенту" in panel.text
    await click(channel, 2, panel, button(panel, "Проверил — разблокировать").callback_data)
    confirm = session.sent[-1]
    assert "Разблокировать диалог" in confirm.text and not released
    await click(channel, 3, confirm, button(confirm, "Работа остановлена").callback_data)
    assert released == [("native-1", "/work")]
    assert "разблокирован" in session.sent[-1].text
    assert jobs(core) == 0 and not server.log
    assert core.active_session(conv).id == record.id
    await channel._command(OWNER, f"/dialogue {record.id}")
    assert "заблокирован" not in session.sent[-1].text
