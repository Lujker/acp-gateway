"""Readable system cards and public progress stay separate from final replies."""

import secrets

from test_telegram import OTHER, OWNER, eventually, feed
from test_telegram import telegram as telegram

from acp_gateway.agents.events import ThoughtChunk
from acp_gateway.channels.telegram_format import plain_html
from acp_gateway.channels.telegram_progress import LIVE_WORK, WORK_HISTORY, WORK_LIMIT
from acp_gateway.core.bus import JobFinished, JobProgress
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, JobStatus
from fakes.telegram import answer_body


def job(core, name="presentation"):
    session = core.store.add_session(Conversation("telegram", str(OWNER), "work"), name, "/work")
    return core.store.add_job(name, session.id)


async def test_menu_has_sections_and_no_duplicate_route_or_dialogue(telegram):
    core, channel, session, server = telegram
    record = core.store.add_session(Conversation("telegram", str(OWNER), "work"), "menu", "/work")
    await feed(channel, 1, "/menu")
    card = session.sent[-1].text
    visible = plain_html(card)
    assert "<b>🏠 Управление ACP Gateway</b>" in card
    assert visible.count("Агент: work") == 1
    assert visible.count(f"«Без названия» #{record.id}") == 1
    assert "Следующий текст →" in visible and "Папка: /work" in visible
    assert not server.log


async def test_progress_is_separate_bounded_redacted_and_survives_channel_cache(telegram):
    core, channel, session, server = telegram
    await feed(channel, 1, "/start")
    current = job(core)
    credential = secrets.token_hex(24)
    register_secret(credential)
    core.bus.publish(
        JobProgress(
            current.conversation,
            current.id,
            ThoughtChunk("native", "Checking <files> " + credential + "\n"),
        )
    )
    core.bus.publish(
        JobProgress(
            current.conversation,
            current.id,
            ThoughtChunk("native", "x" * (WORK_LIMIT + 1)),
        )
    )
    await eventually(lambda: "Checking" in channel._work_snapshot(current)["text"])
    assert not core.job(current.id).answer
    final = core.store.finish_job(current.id, JobStatus.COMPLETED, answer="Final only")
    core.bus.publish(JobFinished(final.conversation, final))
    await eventually(
        lambda: bool(core.store.channel_state(channel.namespace, f"work_history:{OWNER}", {}))
    )
    channel._work_cache().clear()
    snapshot = channel._work_snapshot(final)
    assert len(snapshot["text"]) <= WORK_LIMIT
    assert credential not in snapshot["text"] and snapshot["truncated"]
    await channel._work_panel(OWNER, f"{current.id} 1")
    assert "&lt;files&gt;" in session.sent[-1].text
    assert "продолжение сокращено" in session.sent[-1].text
    assert core.job(current.id).answer == "Final only"
    await feed(channel, 2, f"/work {current.id}", user=OTHER)
    assert "Checking" not in plain_html(session.sent[-1].text)
    assert not server.log


async def test_work_history_failure_does_not_block_answer(telegram, monkeypatch):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    current = job(core)

    def fail(job):
        raise OSError("storage unavailable")

    monkeypatch.setattr(channel, "_finish_work", fail)
    final = core.store.finish_job(
        current.id, JobStatus.COMPLETED, answer="Final despite progress failure"
    )
    core.bus.publish(JobFinished(final.conversation, final))
    await eventually(
        lambda: any(answer_body(m.text) == "Final despite progress failure" for m in session.sent)
    )


async def test_split_bearer_credential_never_leaks_live_or_persisted(telegram):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/start")
    current = job(core)
    credential = "synthetic-private-credential-12345"
    register_secret(credential)
    for text in ["Bearer s", credential[1:]]:
        channel._record_work(
            JobProgress(current.conversation, current.id, ThoughtChunk("native", text))
        )
    assert credential[1:] not in channel._work_snapshot(current)["text"]
    final = core.store.finish_job(current.id, JobStatus.COMPLETED, answer="Final")
    channel._finish_work(final)
    stored = core.store.channel_state(channel.namespace, f"work_history:{OWNER}")[current.id]
    assert credential not in str(stored) and credential[1:] not in str(stored)
    assert "_raw" not in stored and "_source" not in stored


async def test_work_cache_and_persisted_history_have_fixed_bounds(telegram):
    core, channel, _, server = telegram
    await feed(channel, 1, "/start")
    conversation = Conversation("telegram", str(OWNER), "work")
    for index in range(LIVE_WORK + 1):
        channel._record_work(
            JobProgress(conversation, f"live-{index}", ThoughtChunk("native", "step"))
        )
    assert len(channel._work_cache()) == LIVE_WORK
    for index in range(WORK_HISTORY + 1):
        current = job(core, f"history-{index}")
        channel._record_work(JobProgress(conversation, current.id, ThoughtChunk("native", "step")))
        final = core.store.finish_job(current.id, JobStatus.COMPLETED, answer="Final")
        channel._finish_work(final)
    history = core.store.channel_state(channel.namespace, f"work_history:{OWNER}")
    assert len(history) == WORK_HISTORY and "history-0" not in history
    assert not server.log


async def test_jobs_without_work_updates_do_not_create_history(telegram):
    core, channel, _, _ = telegram
    current = job(core)
    final = core.store.finish_job(current.id, JobStatus.COMPLETED, answer="Final")
    channel._finish_work(final)
    assert not channel._work_cache()
    assert core.store.channel_state(channel.namespace, f"work_history:{OWNER}") is None
