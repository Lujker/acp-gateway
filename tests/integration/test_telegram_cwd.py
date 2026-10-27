"""A cwd change creates a new direct-agent dialogue, never rewrites an old binding."""

import time
from unittest.mock import AsyncMock

import pytest
from test_telegram import OWNER, TOKEN
from test_telegram import telegram as telegram

from acp_gateway.agents import AgentError
from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.channels.telegram_format import plain_html
from acp_gateway.core import GatewayError
from acp_gateway.storage import Conversation


def record(core):
    return core.store.add_session(Conversation("telegram", str(OWNER), "work"), "original", "/work")


async def test_new_folder_creates_separate_dialogue_without_prompt_or_old_cwd_change(telegram):
    core, channel, session, server = telegram
    original = record(core)
    await channel._command(OWNER, f"/cwd_hint {original.id}")
    assert "Следующий текст → папка нового диалога" in session.sent[-1].text
    await channel._command(OWNER, "/another/project")
    created = core.active_session(original.conversation)
    assert created.id != original.id and created.cwd == "/another/project"
    assert core.store.session(original.id).cwd == "/work"
    assert not core.job_history("telegram", str(OWNER))["jobs"]
    assert not channel._cwd_input(OWNER)
    assert not any("prompt" in entry for entry in server.log)


@pytest.mark.parametrize("change", ["expired", "selection"])
async def test_expired_or_changed_cwd_form_consumes_text_without_agent_submission(telegram, change):
    core, channel, session, server = telegram
    original = record(core)
    await channel._command(OWNER, f"/cwd_hint {original.id}")
    if change == "expired":
        pending = channel._cwd_input(OWNER)
        pending["expires"] = time.time() - 1
        core.store.set_channel_state(channel.namespace, f"cwd_input:{OWNER}", pending)
    else:
        core.switch_dialogue(original.conversation, original.id)
    await channel._command(OWNER, "/another/project")
    assert "Текст не отправлен" in session.sent[-1].text
    # A path is never offered for sending to the agent.
    assert "Отправить агенту" not in str(session.sent[-1].reply_markup)
    assert len(core.sessions(original.conversation)) == 1 and not server.log
    assert not channel._cwd_input(OWNER)


async def test_persistent_cwd_form_after_channel_restart_and_cancel(telegram):
    from pydantic import SecretStr

    core, channel, session, server = telegram
    original = record(core)
    await channel._command(OWNER, f"/cwd_hint {original.id}")
    fresh = TelegramChannel(channel.settings, SecretStr(TOKEN), bot=channel.bot)
    fresh.core = core
    with pytest.raises(GatewayError, match="абсолютный путь"):
        await fresh._command(OWNER, "relative/path")
    assert fresh._cwd_input(OWNER)
    await fresh._command(OWNER, "/cancel")
    assert "Ввод папки отменён" in session.sent[-1].text
    assert not fresh._cwd_input(OWNER) and not server.log


async def test_cwd_host_failure_is_consumed_without_retry(telegram, monkeypatch):
    core, channel, session, _ = telegram
    original = record(core)
    await channel._command(OWNER, f"/cwd_hint {original.id}")
    create = AsyncMock(side_effect=AgentError("connection dropped"))
    monkeypatch.setattr(core, "new_session", create)
    await channel._command(OWNER, "/another/project")
    assert "Создание диалога не подтверждено" in plain_html(session.sent[-1].text)
    assert create.await_count == 1 and not channel._cwd_input(OWNER)
    assert core.active_session(original.conversation).id == original.id


async def test_connector_folder_does_not_bypass_local_profile_policy(telegram, monkeypatch):
    core, channel, session, server = telegram
    original = record(core)
    client = core.agent("work")
    monkeypatch.setattr(
        client,
        "profile",
        client.profile.model_copy(update={"backend": "connector", "computer_id": "home"}),
    )
    await channel._command(OWNER, f"/folder {original.id}")
    assert "Папка закреплена локальной политикой" in session.sent[-1].text
    with pytest.raises(GatewayError, match="закреплена"):
        await channel._command(OWNER, f"/cwd_hint {original.id}")
    assert not channel._cwd_input(OWNER) and not server.log
