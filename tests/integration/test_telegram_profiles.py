"""Profile forms consume configuration input, never send it to an agent."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from test_telegram import OWNER, eventually, feed
from test_telegram import telegram as telegram

from acp_gateway.connectors.protocol import AgentManifest, ProfileSpec, ProfilesResult
from acp_gateway.storage import Conversation, JobStatus


def enable(channel):
    channel.pairing = SimpleNamespace(owner=Mock(return_value={"telegram_ids": [OWNER]}))
    response = ProfilesResult(
        status="ok",
        revision="a" * 64,
        profiles=[
            ProfileSpec(alias="codex", template="codex", title="Codex", cwd="/work", mode="safe")
        ],
        agents=[
            AgentManifest(alias="codex", display_name="Codex", kind="codex", default_cwd="/work")
        ],
    )
    channel.connector_dispatcher = SimpleNamespace(profiles=AsyncMock(return_value=response))


async def test_profile_form_input_and_confirmation_do_not_start_model(telegram):
    core, channel, session, server = telegram
    enable(channel)
    await channel._command(OWNER, "/profiles home")
    state = channel._profile_drafts[OWNER]
    token = state["token"]
    await channel._command(OWNER, f"/profile_action {token} edit codex")
    await channel._command(OWNER, f"/profile_action {token} cwd")
    assert "Следующий текст → рабочая папка профиля на ПК home" in session.sent[-1].text
    await channel._command(OWNER, "/home/user/new-project")
    assert state["draft"]["cwd"] == "/home/user/new-project"
    await channel._command(OWNER, f"/profile_action {token} save")
    assert "старые диалоги" in session.sent[-1].text
    assert channel.connector_dispatcher.profiles.await_count == 1
    await channel._command(OWNER, f"/profile_action {token} apply")
    req = channel.connector_dispatcher.profiles.await_args.args[1]
    assert req.action == "update" and req.profile.cwd == "/home/user/new-project"
    assert req.actor == f"telegram:{OWNER}"
    assert not core.maintenance_status()["reserved"]
    assert not server.log


async def test_non_owner_is_refused_even_if_telegram_allowlisted(telegram):
    _, channel, _, server = telegram
    enable(channel)
    channel.pairing.owner.side_effect = ValueError("not owner")
    await feed(channel, 1, "/profiles home")
    channel.connector_dispatcher.profiles.assert_not_awaited()
    assert not server.log


async def test_expired_directory_input_is_not_sent_as_prompt(telegram):
    _, channel, _, server = telegram
    enable(channel)
    await channel._command(OWNER, "/profiles home")
    state = channel._profile_drafts[OWNER]
    await channel._command(OWNER, f"/profile_action {state['token']} edit codex")
    await channel._command(OWNER, f"/profile_action {state['token']} cwd")
    state["expires"] = time.time() - 1
    await feed(channel, 1, "/home/user/private-path")
    assert not server.log
    assert channel.connector_dispatcher.profiles.await_count == 1


async def test_cancel_does_not_mutate_connector(telegram):
    _, channel, _, server = telegram
    enable(channel)
    await channel._command(OWNER, "/profiles home")
    state = channel._profile_drafts[OWNER]
    await channel._command(OWNER, f"/profile_action {state['token']} add codex")
    await channel._command(OWNER, "/cancel")
    assert OWNER not in channel._profile_drafts
    assert channel.connector_dispatcher.profiles.await_count == 1
    assert not server.log


@pytest.mark.parametrize("expired", [False, True])
async def test_stop_during_directory_input_still_cancels_active_job(telegram, expired):
    core, channel, _, _ = telegram
    await feed(channel, 1, "/start")
    enable(channel)
    await channel._command(OWNER, "/profiles home")
    state = channel._profile_drafts[OWNER]
    await channel._command(OWNER, f"/profile_action {state['token']} edit codex")
    await channel._command(OWNER, f"/profile_action {state['token']} cwd")
    if expired:
        state["expires"] = time.time() - 1
    job = await core.submit(Conversation("telegram", str(OWNER), "work"), "run exactly: echo hi .")
    await eventually(lambda: bool(core.pending_approvals("telegram")))
    await feed(channel, 2, "/stop")
    assert core.job(job.id).status is JobStatus.CANCELLED


@pytest.mark.parametrize(
    "command", ["/approval id", "/pair_deny id", "/favorite add", "/open_dialogue 1"]
)
async def test_management_and_navigation_commands_take_priority_over_directory_input(
    telegram, command
):
    _, channel, _, _ = telegram
    enable(channel)
    await channel._command(OWNER, "/profiles home")
    state = channel._profile_drafts[OWNER]
    await channel._command(OWNER, f"/profile_action {state['token']} edit codex")
    await channel._command(OWNER, f"/profile_action {state['token']} cwd")
    assert not await channel._profiles(OWNER, command)
    assert OWNER not in channel._profile_drafts


@pytest.mark.parametrize("remaining", [120.0, 5.0])
async def test_rpc_expiry_has_clock_headroom_and_never_outlives_form(
    telegram, monkeypatch, remaining
):
    import acp_gateway.channels.telegram_profiles as module

    _, channel, _, server = telegram
    enable(channel)
    now = time.time()
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: now))
    state = {"computer": "home", "operation": uuid4(), "expires": now + remaining}
    await channel._profiles_rpc(OWNER, state, "list")
    req = channel.connector_dispatcher.profiles.await_args.args[1]
    connector_now = now - 6.4
    assert connector_now <= req.expires <= connector_now + 120
    assert req.expires <= now + 30
    assert req.expires <= state["expires"]
    assert not server.log


async def test_discovered_agent_can_be_added_on_hermes_only_pc_without_model(telegram):
    from acp_gateway.connectors.protocol import AgentCandidate

    core, channel, session, server = telegram
    enable(channel)
    discovered = ProfilesResult(
        status="ok",
        revision="a" * 64,
        candidates=[
            AgentCandidate(
                id="a" * 32, kind="codex", title="Codex / Ubuntu", default_cwd="/home/alice"
            ),
        ],
    )
    channel.connector_dispatcher.profiles.side_effect = [
        discovered,
        ProfilesResult(status="ok", initialization="ok"),
    ]
    await channel._command(OWNER, "/profiles home discover")
    state = channel._profile_drafts[OWNER]
    assert not state["profiles"]
    token = state["token"]
    assert not core.maintenance_status()["reserved"]
    await channel._command(OWNER, f"/profile_action {token} candidate {'a' * 32}")
    await channel._command(OWNER, "/home/alice/project")
    await channel._command(OWNER, f"/profile_action {token} work")
    await channel._command(OWNER, f"/profile_action {token} save")
    assert channel.connector_dispatcher.profiles.await_count == 1
    await channel._command(OWNER, f"/profile_action {token} apply")
    req = channel.connector_dispatcher.profiles.await_args.args[1]
    assert req.action == "add" and req.profile.candidate == "a" * 32
    assert req.profile.template is None and req.profile.mode == "work"
    assert req.profile.cwd == "/home/alice/project" and req.profile.alias == "codex"
    assert "Initialize пройден" in session.sent[-1].text
    assert not core.maintenance_status()["reserved"]
    assert not server.log


async def test_empty_discovery_has_local_installation_hint(telegram):
    _, channel, session, server = telegram
    enable(channel)
    channel.connector_dispatcher.profiles.return_value = ProfilesResult(
        status="ok",
        candidates=[],
        hints=["Ubuntu: установите Codex CLI локально"],
    )
    await channel._command(OWNER, "/profiles home discover")
    assert "Агенты не найдены" in session.sent[-1].text
    assert "установите Codex CLI" in session.sent[-1].text
    assert not server.log
