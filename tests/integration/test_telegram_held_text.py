"""A rejected text is sent only by an explicit, one-shot button bound to the same route."""

from test_telegram import OWNER, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button
from test_telegram_rename import begin

from acp_gateway.agents import SessionBusy
from acp_gateway.storage import Conversation


def jobs(core):
    return core.store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


async def test_busy_dialogue_keeps_text_for_an_explicit_send(telegram, monkeypatch):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    core.store.add_session(conv, "native-1", "/work")
    real = core.submit_selected
    busy = [True]
    sent = []

    async def submit(conversation, text, **kwargs):
        if busy[0]:
            raise SessionBusy("work is still working on the previous request in this session")
        sent.append(text)
        return await real(conversation, text, **kwargs)

    monkeypatch.setattr(core, "submit_selected", submit)
    await feed(channel, 1, "say pong")
    panel = session.sent[-1]
    assert "Отправить агенту" in panel.text
    assert "не отправлено агенту" in panel.text and jobs(core) == 0
    data = button(panel, "Отправить агенту").callback_data
    busy[0] = False
    await click(channel, 2, panel, data)
    assert sent == ["say pong"] and jobs(core) == 1
    await click(channel, 3, panel, data)  # one-shot: never sent twice
    assert sent == ["say pong"] and jobs(core) == 1


async def test_held_text_is_not_sent_to_a_different_dialogue(telegram, monkeypatch):
    core, channel, session, _ = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    core.store.add_session(conv, "native-1", "/work")

    attempts = []

    async def submit(conversation, text, **kwargs):
        attempts.append(text)
        raise SessionBusy("work is still working on the previous request in this session")

    monkeypatch.setattr(core, "submit_selected", submit)
    await feed(channel, 1, "say pong")
    panel = session.sent[-1]
    core.store.add_session(conv, "native-2", "/work")  # selection changes
    sent_before = len(session.sent)
    await click(channel, 2, panel, button(panel, "Отправить агенту").callback_data)
    # The stale button is refused before anything reaches the new dialogue.
    assert attempts == ["say pong"]
    assert len(session.sent) == sent_before and jobs(core) == 0


async def test_text_after_expired_rename_can_be_sent_explicitly(telegram):
    core, channel, session, _ = telegram
    await begin(core, channel, session)
    pending = channel._rename_input(OWNER)
    pending["expires"] = 0
    core.store.set_channel_state(channel.namespace, f"rename_input:{OWNER}", pending)
    await feed(channel, 3, "say pong")
    panel = session.sent[-1]
    assert "не отправлено агенту" in panel.text and jobs(core) == 0
    await click(channel, 4, panel, button(panel, "Отправить агенту").callback_data)
    assert jobs(core) == 1
