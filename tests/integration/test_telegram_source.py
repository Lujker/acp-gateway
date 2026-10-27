"""Result source evidence and automatic presentation remain stable across route changes."""

import hashlib
import json
from datetime import timedelta

from test_telegram import OWNER, feed
from test_telegram import telegram as telegram
from test_telegram_delivery import pause_sender, saved

from acp_gateway.channels.telegram_format import FORMAT_VERSION, plain_html, result_chunks
from acp_gateway.storage import Conversation
from acp_gateway.storage.records import utcnow


async def test_source_snapshot_precedes_later_model_title_and_selection_changes(telegram):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/new")
    conv = Conversation("telegram", str(OWNER), "work")
    original = core.active_session(conv)
    core.rename_dialogue(conv, original.id, "Original")
    job = await core.submit_selected(conv, "say pong")
    await core.wait(job.id, timeout=5)
    core.rename_dialogue(conv, original.id, "Renamed")
    # Goose later reports another model (config_option_update); the old answer keeps its own.
    client = core.agent("work")
    client._observations[original.acp_session_id] = {
        "provider": "provider-b",
        "model": "model-b",
        "effort": None,
        "source": "acp.config_options",
        "observed_at": utcnow().isoformat(),
    }
    assert core.observed_model(core.active_session(conv))["observed_model"] == "model-b"
    other = core.store.add_session(conv, "other", "/other")
    footer = channel._result_footer(OWNER, job)
    assert "Original" in footer and "Renamed" not in footer
    # Recorded Goose 1.53 session/new reports provider, model and thinking effort.
    assert "Goose · chatgpt_codex · gpt-5.6-sol · medium" in footer
    assert "model-b" not in footer
    assert f"#{other.id}" in footer and f"#{original.id}" in footer
    assert core.store.job_context(job.id)["cwd"] == "/work"
    core.store.prune_jobs(utcnow() + timedelta(seconds=1))
    assert core.store.job_context(job.id) is None


async def test_pinned_source_presentation_survives_selection_change_after_ack(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    job = saved(core, channel, "A" * 5000)
    await channel._deliver_result(core.store.deliveries.due(channel.namespace))
    initial = core.store.deliveries.get(channel.namespace, job.id)
    core.store.add_session(job.conversation, "new-selection", "/new-selection")
    while (row := core.store.deliveries.due(channel.namespace)) is not None:
        await channel._deliver_result(row)
    final = core.store.deliveries.get(channel.namespace, job.id)
    assert final["state"] == "delivered"
    assert final["presentation"] == initial["presentation"]
    assert final["content_hash"] == initial["content_hash"]
    replies = [m for m in session.sent if "📄 Ответ ·" in m.text]
    assert len(replies) == final["next_chunk"]
    assert all("/new-selection" not in plain_html(m.text) for m in replies)


async def test_started_v2_delivery_keeps_its_original_unsigned_boundaries(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/start")
    await pause_sender(channel)
    text = "words " * 1000
    job = saved(core, channel, text)
    chunks = result_chunks(text)
    digest = hashlib.sha256(
        json.dumps([FORMAT_VERSION, chunks], ensure_ascii=True).encode()
    ).hexdigest()
    deliveries = core.store.deliveries
    assert deliveries.claim(channel.namespace, job.id)
    assert deliveries.bind_content(channel.namespace, job.id, digest)
    await channel._send_payload(OWNER, chunks[0])
    deliveries.acknowledge(channel.namespace, job.id, last=False)
    while (row := deliveries.due(channel.namespace)) is not None:
        await channel._deliver_result(row)
    replies = [m.text for m in session.sent if m.text in chunks]
    assert replies == chunks
    assert deliveries.get(channel.namespace, job.id)["state"] == "delivered"
    assert deliveries.get(channel.namespace, job.id)["presentation"] is None
