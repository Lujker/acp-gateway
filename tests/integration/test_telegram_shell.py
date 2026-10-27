"""Telegram direct shell never becomes an implicit model turn or navigation card."""

import asyncio
import sys

import pytest
from aiogram.exceptions import TelegramNetworkError
from test_shell_dispatcher import shell as shell
from test_telegram import OTHER, OWNER, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.adapters.managed_sessions import ManagedSessions
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.protocol import AgentManifest
from acp_gateway.connectors.shell_service import LocalShell
from acp_gateway.core import GatewayError
from acp_gateway.storage import Conversation


async def test_bang_creates_source_card_without_model_then_edits_pages_and_keeps_menu(
    shell, telegram
):
    coordinator, conv, remote = shell
    core, channel, session, server = telegram
    remote.output = "<first>" + "a" * 700 + "<last>"
    await feed(channel, 1, "!echo test")
    assert len(remote.frames) == 1
    row = coordinator.rows()[0]
    assert not core.running_jobs()
    assert not [e for e in server.log if "prompt" in str(e).lower()]
    original = session.sent[-1]
    assert "&lt;first&gt;" in original.text and "&amp;lt;first" not in original.text
    assert "home" in original.text and "Результат не отправлен агенту" in original.text
    await click(channel, 2, original, button(original, "Вывод →").callback_data)
    assert len(session.sent) == 1
    assert "&lt;last&gt;" in session.sent[0].text
    await click(channel, 3, session.sent[0], button(session.sent[0], "← Меню").callback_data)
    assert len(session.sent) == 2 and "Shell ·" in session.sent[0].text
    assert coordinator.owned(row["id"], OWNER)["message_id"] == original.message_id
    assert core.active_session(conv).id == row["dialogue"]


async def test_refusal_does_not_create_model_job_and_input_forms_take_priority(telegram, shell):
    _, conv, remote = shell
    core, channel, session, _ = telegram
    remote.capable = False
    await feed(channel, 1, "!echo must-not-reach-model")
    assert "поддерживает" in session.sent[-1].text
    assert not remote.frames and not core.running_jobs()
    remote.capable = True
    await channel._command(OWNER, f"/rename_hint {core.active_session(conv).id}")
    await channel._command(OWNER, "!dialogue title")
    assert core.active_session(conv).title == "!dialogue title"
    assert not remote.frames


async def test_changed_selection_blocks_old_send_but_explicit_activation_restores_source(
    shell, telegram
):
    coordinator, conv, _ = shell
    core, channel, session, _ = telegram
    await channel._command(OWNER, "!echo")
    row = coordinator.rows()[0]
    card = session.sent[-1]
    send_token = button(card, "Передать агенту").callback_data
    other = core.store.add_session(conv, "another", "/other")
    await click(channel, 1, card, send_token)
    assert not core.running_jobs()
    assert coordinator.owned(row["id"], OWNER)["transfer_state"] == "available"
    await channel._shell_card(OWNER, row["id"])
    card = session.sent[0]
    assert "Исходный диалог не активен" in card.text and f"#{other.id}" in card.text
    await click(channel, 2, card, button(card, "Сделать активным").callback_data)
    assert core.active_session(conv).id == row["dialogue"]
    card = session.sent[0]
    send_token = button(card, "Передать агенту").callback_data
    await click(channel, 3, card, send_token)
    sent = coordinator.owned(row["id"], OWNER)
    assert sent["transfer_state"] == "sent"
    await core.wait(sent["transfer_job"], 5)
    await click(channel, 4, card, send_token)
    assert coordinator.owned(row["id"], OWNER)["transfer_job"] == sent["transfer_job"]


async def test_ambiguous_card_send_is_not_automatically_duplicated_and_owner_is_checked(
    shell, telegram
):
    coordinator, conv, _ = shell
    _, channel, session, _ = telegram
    row = await coordinator.start(conv, "echo")
    session.failures.append(lambda method: TelegramNetworkError(method, "lost"))
    with pytest.raises(TelegramNetworkError):
        await channel._shell_card(OWNER, row["id"], explicit=True)
    assert coordinator.owned(row["id"], OWNER)["card_state"] == "unknown"
    await channel._shell_card(OWNER, row["id"])
    assert not session.sent
    with pytest.raises(GatewayError):
        await channel._command(OTHER, f"/shell {row['id']}")
    await channel._command(OWNER, "/shells")
    listing = session.sent[-1]
    await click(channel, 1, listing, button(listing, "Завершена").callback_data)
    assert len(session.sent) == 2 and "Shell ·" in session.sent[-1].text


async def test_restart_rebinds_known_card_without_new_message_or_start(shell, telegram):
    coordinator, _, remote = shell
    _, channel, session, _ = telegram
    await channel._command(OWNER, "!echo")
    row = coordinator.rows()[0]
    channel._shell_bound.clear()
    coordinator.recover()
    await channel._shell_card(OWNER, row["id"])
    assert len(session.sent) == len(remote.frames) == 1
    assert button(session.sent[0], "Передать агенту")
    await channel._command(OWNER, "/shells")
    assert "Shell-операции" in session.sent[-1].text


async def test_background_reads_status_and_edits_original_without_resubmission(shell, telegram):
    coordinator, _, remote = shell
    _, channel, session, _ = telegram
    remote.fail = True
    await channel._command(OWNER, "!echo")
    row = coordinator.rows()[0]
    assert "Исход неизвестен" in session.sent[0].text
    row["updated"] = 0
    coordinator.save(row)
    remote.fail = False
    # Exercise a full worker iteration without waiting for its next timer.
    task = asyncio.create_task(channel._shell_updates())
    try:
        async with asyncio.timeout(5):
            while "Завершена" not in session.sent[0].text:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(session.sent) == 1
    assert [f.action for f in remote.frames] == ["start", "status"]
    assert coordinator.owned(row["id"], OWNER)["cleanup_verified"]
    assert coordinator.core.active_session(Conversation("telegram", str(OWNER), "work"))


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux shell process")
async def test_telegram_dispatcher_websocket_and_local_shell_execute_once(
    shell, telegram, tmp_path
):
    from test_connector_update_inventory import connected

    coordinator, conv, _ = shell
    core, channel, session, _ = telegram
    profile = AgentProfile(
        alias="code",
        kind="codex",
        backend="codex",
        default_cwd=str(tmp_path),
        codex={"mode": "work"},
    )
    bindings = ManagedSessions(tmp_path / "bindings", profile)
    bindings.create("owned", str(tmp_path))
    core.store.add_session(conv, "owned", str(tmp_path))
    service = LocalShell(
        tmp_path / "shell.db",
        profiles=lambda: [profile],
        authorize=lambda alias, sid, cwd: bindings.contains(sid, cwd),
        admission=lambda: None,
    )
    try:
        async with connected(
            tmp_path / "wire",
            shell_operations=True,
            shell_handler=service.request,
            agents=[AgentManifest(alias="code", display_name="Code")],
        ) as (remote, _, _):
            channel.connector_dispatcher = coordinator.dispatcher = remote
            await channel._command(OWNER, "!printf once >> count.txt; printf '<done>'")
            row = coordinator.rows()[0]
            async with asyncio.timeout(5):
                while not row["remote_terminal"]:
                    row = await coordinator.refresh(OWNER, row["id"])
                    await asyncio.sleep(0.01)
            await channel._shell_card(OWNER, row["id"])
            await channel._command(OWNER, f"/shell {row['id']}")
            assert (tmp_path / "count.txt").read_text() == "once"
            assert len(session.sent) == 1 and "&lt;done&gt;" in session.sent[0].text
            assert row["cleanup_verified"] and not core.running_jobs()
    finally:
        await service.close()
        bindings.close()
