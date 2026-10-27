"""History/status are local, owned, paginated views independent of selection."""

import secrets
from datetime import timedelta

import pytest
from test_telegram import OTHER, OWNER, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.channels.telegram_format import plain_html
from acp_gateway.core import GatewayError
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, JobStatus, Store
from acp_gateway.storage.records import utcnow


async def test_new_prompt_is_saved_before_execution_and_history_survives_reopen(telegram, tmp_path):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    job = await core.submit_selected(conv, "say pong")
    assert core.store.dialogue_turn(job.session_id, job.id)["turn"]["prompt"] == "say pong"
    await core.wait(job.id, timeout=5)
    reopened = Store.open(tmp_path / "gateway.db")
    try:
        turn = reopened.dialogue_turn(job.session_id, job.id)["turn"]
        assert turn["prompt"] == "say pong" and turn["answer"] == "pong"
    finally:
        reopened.close()


async def test_history_navigation_is_private_and_keeps_selection_and_list_page(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    records = [core.store.add_session(conv, f"s{i}", "/work") for i in range(12)]
    record = records[0]
    core.store.add_job("a", record.id)
    core.store.finish_job("a", JobStatus.COMPLETED, answer="Old answer")
    core.store.add_job("b", record.id, prompt="New query")
    core.store.finish_job("b", JobStatus.COMPLETED, answer="New answer")
    active, revision = core.active_session(conv).id, core.store.active_revision(conv)
    await channel._command(OWNER, f"/dialogue {record.id} 2")
    preview = session.sent[-1]
    await click(channel, 1, preview, button(preview, "История").callback_data)
    assert "New query" in session.sent[-1].text
    await click(channel, 2, session.sent[-1], button(session.sent[-1], "Раньше").callback_data)
    assert "Исходный запрос не сохранён" in session.sent[-1].text
    assert "Old answer" in session.sent[-1].text
    await click(channel, 3, session.sent[-1], button(session.sent[-1], "← Диалог").callback_data)
    await click(channel, 4, session.sent[-1], button(session.sent[-1], "← Диалоги").callback_data)
    assert "Страница 2/2" in session.sent[-1].text and len(session.sent) == 1
    assert core.active_session(conv).id == active and core.store.active_revision(conv) == revision
    with pytest.raises(GatewayError, match="does not own"):
        await channel._command(OTHER, f"/history {record.id}")
    assert not server.log


async def test_history_fragments_redact_before_splitting_and_pin_exact_job(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "history", "/work")
    secret = secrets.token_hex(24)
    register_secret(secret)
    core.store.add_job("original", record.id, prompt="<query> " + "a" * 1000)
    core.store.finish_job(
        "original", JobStatus.COMPLETED, answer="<answer> " + "b" * 2100 + secret + " END-OF-TURN"
    )
    await channel._command(OWNER, f"/history {record.id}")
    assert "&lt;query&gt;" in session.sent[-1].text
    core.store.add_job("z-new", record.id, prompt="NEW ARRIVAL")
    texts = []
    while True:
        card = session.sent[-1]
        texts.append(plain_html(card.text))
        buttons = [
            b for row in card.reply_markup.inline_keyboard for b in row if b.text == "Текст →"
        ]
        if not buttons:
            break
        await click(channel, len(texts), card, buttons[0].callback_data)
    joined = "\n".join(texts)
    assert "END-OF-TURN" in joined and "<answer>" in joined
    assert "NEW ARRIVAL" not in joined and secret not in joined
    assert len(session.sent) == 1 and not server.log
    core.store.prune_jobs(utcnow() + timedelta(seconds=1))
    with pytest.raises(GatewayError, match="expired"):
        await channel._command(OWNER, f"/history {record.id} original")


async def test_status_filters_jobs_by_dialogue_without_initializing_agent(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    first = core.store.add_session(conv, "one", "/one")
    second = core.store.add_session(conv, "two", "/two")
    await channel._command(OWNER, f"/dialogue_status {first.id}")
    assert "Активных задач шлюза: 0" in session.sent[-1].text
    assert "не счётчик живых процессов" in session.sent[-1].text
    assert core.active_session(conv).id == second.id and not server.log
    foreign = core.store.add_session(
        Conversation("telegram", str(OTHER), "work"), "other", "/other"
    )
    core.store.add_job("foreign", foreign.id, prompt="PRIVATE")
    with pytest.raises(GatewayError):
        await channel._command(OWNER, f"/history {first.id} foreign")
    assert "PRIVATE" not in session.sent[-1].text
