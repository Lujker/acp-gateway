"""Actual ACP/native subprocess roundtrips for per-dialogue model evidence."""

import pytest
from aiogram import Bot
from pydantic import SecretStr
from test_codex_managed import calls
from test_codex_managed import peer as peer
from test_telegram import OWNER, TOKEN

from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.config import TelegramSettings
from acp_gateway.core import GatewayCore
from acp_gateway.storage import Conversation, JobStatus, Store
from fakes.telegram import FakeTelegramSession


@pytest.mark.parametrize("scenario", ["observation", "observation-invalid", "happy"])
async def test_native_observation_is_projected_without_config_or_catalog_inference(peer, scenario):
    client = peer(scenario, model="configured-is-not-evidence")
    try:
        sid = await client.new_session()
        value = client.session_observation(sid)
        assert value["source"] == "codex.thread.start"
        assert value["model"] == ("native-model" if scenario == "observation" else None)
        assert value["effort"] == ("high" if scenario == "observation" else None)
        assert "configured-is-not-evidence" not in str(value)
        await client.load_session(sid)
        assert client.session_observation(sid)["model"] == value["model"]
    finally:
        await client.close()


@pytest.mark.parametrize("missing", [False, True])
async def test_resume_updates_only_same_dialogue_and_snapshot_is_before_turn(
    peer, tmp_path, missing
):
    client = peer("observation-missing-resume" if missing else "observation")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"code": client})
    conv = Conversation("telegram", str(OWNER), "code")
    await core.start()
    try:
        first = await core.new_session(conv)
        assert core.observed_model(first)["observed_model"] == "native-model"
        await client.close()
        job = await core.submit_selected(conv, "hello")
        assert (await core.wait(job.id, 5)).status is JobStatus.COMPLETED
        context = core.store.job_context(job.id)
        assert context["native_model_pinned"]
        assert context["model"] == (None if missing else "resumed-model")
        assert context["effort"] is None
        assert context["model_observation_source"] == "codex.thread.resume"
        current = core.observed_model(first)
        assert current["observed_model"] == context["model"]
        # Read-only native evidence does not settle/confirm a setter record.
        assert core.store.models.state(first.id) is None
        native_calls = calls(tmp_path)
        assert [f["method"] for f in native_calls].count("turn/start") == 1
        reopened = Store.open(tmp_path / "gateway.db")
        try:
            assert reopened.job_context(job.id) == context
            assert (
                reopened.channel_state("native_model_observation", str(first.id))["value"]["model"]
                == context["model"]
            )
        finally:
            reopened.close()
    finally:
        await core.close()


async def test_telegram_context_and_saved_result_keep_effort_after_resume(peer, tmp_path):
    client = peer("observation")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"code": client})
    conv = Conversation("telegram", str(OWNER), "code")
    channel = TelegramChannel(
        TelegramSettings(enabled=True, allowed_user_ids=[OWNER]),
        SecretStr(TOKEN),
        bot=Bot(TOKEN, session=FakeTelegramSession()),
    )
    channel.core = core
    await core.start()
    try:
        first = await core.new_session(conv)
        assert "native-model · high" in channel._destination_text(OWNER)
        assert "native-model · high" in channel._context_text(OWNER)
        job = await core.submit_selected(conv, "hello")
        await core.wait(job.id, 5)
        await client.close()
        await client.load_session(first.acp_session_id, first.cwd)
        assert "resumed-model" in channel._destination_text(OWNER)
        await channel._command(OWNER, "/model state")
        assert "resumed-model" in channel.bot.session.sent[-1].text
        assert "Сохранённых наблюдений пока нет" not in channel.bot.session.sent[-1].text
        footer = channel._result_footer(OWNER, job)
        assert "native-model · high" in footer and "resumed-model" not in footer
        assert "custom-provider" in footer
    finally:
        await core.close()
        await channel.bot.session.close()


async def test_parallel_dialogues_and_reopened_dispatcher_keep_separate_evidence(peer, tmp_path):
    client = peer("observation-parallel")
    db = tmp_path / "gateway.db"
    core = GatewayCore(Store.open(db), {"code": client})
    conv = Conversation("telegram", str(OWNER), "code")
    await core.start()
    try:
        first, second = await core.new_session(conv), await core.new_session(conv)
        models = [core.observed_model(r)["observed_model"] for r in (first, second)]
        assert models[0] != models[1]
        core.switch_dialogue(conv, first.id)
        assert core.observed_model(core.active_session(conv))["observed_model"] == models[0]
    finally:
        await core.close()
    count = len(calls(tmp_path))
    restarted = GatewayCore(Store.open(db), {"code": peer("observation-parallel")})
    await restarted.start()
    try:
        assert restarted.observed_model(first)["observed_model"] == models[0]
        assert restarted.observed_model(second)["observed_model"] == models[1]
        assert not restarted.agent("code").connected
        assert len(calls(tmp_path)) == count
    finally:
        await restarted.close()


async def test_native_observation_does_not_clear_unconfirmed_setter(peer, tmp_path):
    client = peer("observation")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"code": client})
    conv = Conversation("telegram", str(OWNER), "code")
    await core.start()
    try:
        record = await core.new_session(conv)
        core.store.models.begin(record.id, "custom-provider", "resumed-model", 1)
        await client.close()
        await client.load_session(record.acp_session_id, record.cwd)
        assert core.active_session(conv).availability == "uncertain"
        assert core.observed_model(record)["observed_model"] == "resumed-model"
        assert core.store.models.state(record.id)["state"] == "intent"
    finally:
        await core.close()


async def test_snapshot_failure_prevents_model_turn_and_does_not_leave_busy_fence(
    peer, tmp_path, monkeypatch
):
    client = peer("observation")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"code": client})
    conv = Conversation("telegram", str(OWNER), "code")
    await core.start()
    try:
        first = await core.new_session(conv)

        def failed(*args):
            raise OSError("snapshot unavailable")

        monkeypatch.setattr(core.store, "observe_job_model", failed)
        job = await core.submit_selected(conv, "do not send")
        assert (await core.wait(job.id, 5)).status is JobStatus.FAILED
        assert not client._turns
        assert not [f for f in calls(tmp_path) if f.get("method") == "turn/start"]
        assert core.active_session(conv).id == first.id
    finally:
        await core.close()
