"""Model-free Claude init evidence through native stream, ACP and core storage."""

import asyncio
import secrets

import pytest
from aiogram import Bot
from pydantic import SecretStr
from test_claude_code_managed import calls
from test_claude_code_managed import peer as peer
from test_telegram import OWNER, TOKEN

from acp_gateway.agents.client import AgentClient
from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.config import AgentProfile, TelegramSettings
from acp_gateway.core import GatewayCore
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, JobStatus, Store
from fakes.telegram import FakeTelegramSession


async def client_for(peer, tmp_path, scenario):
    stream = await peer(scenario)

    async def factory():
        return stream

    return AgentClient(
        AgentProfile(
            alias="claude",
            kind="claude_code",
            backend="claude_code",
            default_cwd=str(tmp_path),
            claude_code=stream.settings,
        ),
        pin_dir=tmp_path,
        request_timeout=2,
        reconnect_delays=(),
        transport_factory=factory,
    )


@pytest.mark.parametrize(
    "scenario", ["observation", "observation-invalid", "observation-foreign-only", "happy"]
)
async def test_native_init_is_scoped_and_invalid_or_missing_values_stay_unknown(
    peer, tmp_path, scenario
):
    client = await client_for(peer, tmp_path, scenario)
    try:
        sid = await client.new_session()
        assert client.session_observation(sid) is None  # Claude reports its model after input
        await client.ask(sid, "hello")
        value = client.session_observation(sid)
        assert value["model"] == ("native-" + sid if scenario == "observation" else None)
        assert value["provider"] is value["effort"] is None
        if scenario == "observation":
            await client.ask(sid, "next")
            assert client.session_observation(sid)["model"] == "native-" + sid
        assert "foreign-model" not in str(value) and "subagent-model" not in str(value)
    finally:
        await client.close()


async def test_parallel_dialogues_keep_model_evidence_and_answer_footers_separate(peer, tmp_path):
    client = await client_for(peer, tmp_path, "observation")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"claude": client})
    conv = Conversation("telegram", str(OWNER), "claude")
    channel = TelegramChannel(
        TelegramSettings(enabled=True, allowed_user_ids=[OWNER]),
        SecretStr(TOKEN),
        bot=Bot(TOKEN, session=FakeTelegramSession()),
    )
    channel.core = core
    await core.start()
    try:
        first, second = await core.new_session(conv), await core.new_session(conv)
        a, b = (
            await core.submit(conv, "one", acp_session_id=first.acp_session_id),
            await core.submit_selected(conv, "two"),
        )
        await asyncio.gather(core.wait(a.id, 5), core.wait(b.id, 5))
        for record, job in ((first, a), (second, b)):
            expected = "native-" + record.acp_session_id
            assert core.observed_model(record)["observed_model"] == expected
            context = core.store.job_context(job.id)
            assert context["model"] == expected and context["native_model_pinned"]
            assert context["provider"] is context["effort"] is None
            footer = channel._result_footer(OWNER, job)
            assert expected in footer and "наблюдение native init" in footer
            assert "наблюдение перед запросом" not in footer
        assert "native-" + second.acp_session_id in channel._context_text(OWNER)
        third = await core.submit_selected(conv, "third")
        await core.wait(third.id, 5)
        assert core.store.job_context(a.id)["model"] == "native-" + first.acp_session_id
        assert core.store.models.state(first.id) is None
        reopened = Store.open(tmp_path / "gateway.db")
        try:
            assert reopened.job_context(a.id)["model"] == "native-" + first.acp_session_id
        finally:
            reopened.close()
    finally:
        await core.close()
        await channel.bot.session.close()


async def test_connector_relay_preserves_claude_init_metadata(peer, tmp_path):
    from test_connector_update_inventory import connected

    from acp_gateway.connectors.policy import LocalAgentPolicy, PolicyTransport
    from acp_gateway.connectors.protocol import AgentManifest

    async with connected(
        tmp_path / "wire",
        agents=[AgentManifest(alias="claude", display_name="Claude")],
    ) as (dispatcher, relay, _):
        stream = await peer("observation")

        async def factory(alias):
            assert alias == "claude"
            return PolicyTransport(stream, LocalAgentPolicy((str(tmp_path),)))

        relay.local_factory = factory
        remote = AgentClient(
            AgentProfile(
                alias="claude",
                kind="claude_code",
                backend="connector",
                computer_id="home",
                default_cwd=str(tmp_path),
            ),
            pin_dir=tmp_path,
            reconnect_delays=(),
            transport_factory=lambda: dispatcher.open_agent("home", "claude"),
        )
        try:
            sid = await remote.new_session()
            assert (await remote.ask(sid, "hello")).text == "hello"
            assert remote.session_observation(sid)["model"] == "native-" + sid
            assert remote.session_observation(sid)["provider"] is None
        finally:
            await remote.close()
            await stream.close()


async def test_post_turn_storage_failure_does_not_replay_completed_model_request(
    peer, tmp_path, monkeypatch
):
    client = await client_for(peer, tmp_path, "observation")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"claude": client})
    conv = Conversation("telegram", str(OWNER), "claude")
    await core.start()
    try:
        await core.new_session(conv)
        original = core.store.observe_job_model

        def failed(*args):
            raise OSError("storage unavailable")

        monkeypatch.setattr(core.store, "observe_job_model", failed)
        job = await core.submit_selected(conv, "once")
        assert (await core.wait(job.id, 5)).status is JobStatus.FAILED
        assert len([c for c in calls(tmp_path) if c["frame"].get("type") == "user"]) == 1
        assert not client._turns
        monkeypatch.setattr(core.store, "observe_job_model", original)
        next_job = await core.submit_selected(conv, "next")
        assert (await core.wait(next_job.id, 5)).answer == "once|next"
        assert len([c for c in calls(tmp_path) if c["frame"].get("type") == "user"]) == 2
    finally:
        await core.close()


async def test_native_error_remains_failed_even_when_model_is_observed(peer, tmp_path):
    client = await client_for(peer, tmp_path, "observation-error")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"claude": client})
    conv = Conversation("telegram", str(OWNER), "claude")
    await core.start()
    try:
        record = await core.new_session(conv)
        job = await core.submit_selected(conv, "fail")
        assert (await core.wait(job.id, 5)).status is JobStatus.FAILED
        assert core.store.job_context(job.id)["model"] == "native-" + record.acp_session_id
    finally:
        await core.close()


async def test_secret_native_model_never_enters_saved_observation(peer, tmp_path):
    credential = secrets.token_hex(32)
    register_secret(credential)
    client = await client_for(peer, tmp_path, "observation-secret")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"claude": client})
    conv = Conversation("telegram", str(OWNER), "claude")
    await core.start()
    try:
        record = await core.new_session(conv)
        job = await core.submit_selected(conv, credential)
        assert (await core.wait(job.id, 5)).status is JobStatus.COMPLETED
        assert core.store.job_context(job.id)["model"] is None
        assert credential not in str(core.store.job_context(job.id))
        assert credential not in str(core.observed_model(record))
    finally:
        await core.close()


async def test_first_init_after_result_is_not_attributed_to_finished_turn(peer, tmp_path):
    client = await client_for(peer, tmp_path, "observation-late")
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {"claude": client})
    conv = Conversation("telegram", str(OWNER), "claude")
    await core.start()
    try:
        await core.new_session(conv)
        first = await core.submit_selected(conv, "first")
        await core.wait(first.id, 5)
        original = core.store.job_context(first.id)
        assert original["model"] is None
        second = await core.submit_selected(conv, "second")
        await core.wait(second.id, 5)
        assert core.store.job_context(second.id)["model"] == "late-duplicate-model"
        assert core.store.job_context(first.id) == original
    finally:
        await core.close()


async def test_reattach_without_init_keeps_last_known_model_but_a_turn_does_not(peer, tmp_path):
    client = await client_for(peer, tmp_path, "observation")
    try:
        sid = await client.new_session()
        await client.ask(sid, "hello")
        bare = type("Bare", (), {"field_meta": None})()
        client._remember_observation(sid, bare)  # e.g. session/load after reconnect
        assert client.session_observation(sid)["model"] == "native-" + sid
        client._remember_observation(sid, bare, turn=True)  # a turn without its own init
        assert client.session_observation(sid)["model"] is None
    finally:
        await client.close()
