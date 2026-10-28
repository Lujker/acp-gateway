# ruff: noqa: RUF001
"""Human update inventory through the real Telegram router and panel controls."""

from types import SimpleNamespace

import httpx
import pytest
from packaging.version import Version
from test_telegram import click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.connectors.protocol import InstallationReport
from acp_gateway.updates import UpdateInventory


def report(manager="docker"):
    return InstallationReport(
        installed_version="0.2.0", manager=manager, source="registry", platform="linux"
    )


async def test_menu_versions_and_explicit_check_are_read_only(telegram):
    _, channel, session, server = telegram
    checks = []
    channel.update_inventory = UpdateInventory(
        package_lookup=lambda version: True,
        local_report=report,
        release_lookup=lambda: checks.append(1) or Version("0.3.0"),
        image_lookup=lambda version: None,
    )
    await feed(channel, 1, "/menu")
    menu = session.sent[-1]
    await click(channel, 2, menu, button(menu, "Обновления").callback_data)
    assert "Диспетчер: 0.2.0" in session.sent[-1].text
    assert "внешний updater" in session.sent[-1].text
    assert "локального включения" not in session.sent[-1].text
    assert "автоматически" in session.sent[-1].text
    assert not checks
    card = session.sent[-1]
    await click(channel, 3, card, button(card, "Проверить версии").callback_data)
    assert "0.3.0" in session.sent[-1].text and checks == [1]
    assert "не опубликован" in session.sent[-1].text
    assert not server.log


@pytest.mark.parametrize("latest,published", [("0.2.0", True), ("0.3.0", False)])
async def test_manual_update_check_explains_pypi_cache_and_updater(telegram, latest, published):
    _, channel, session, _ = telegram
    channel.update_inventory = UpdateInventory(
        local_report=lambda: report("uv"),
        release_lookup=lambda: Version(latest),
        package_lookup=lambda version: published,
    )
    await feed(channel, 1, "/updates_check")
    text = "\n".join(message.text for message in session.sent)
    assert "PyPI" in text and "повторите проверку позже" in text
    assert "Updater недоступен" in text


@pytest.mark.parametrize("case", ["new", "old", "timeout"])
async def test_failed_update_details_respect_capability_and_timeout(telegram, case):
    import asyncio

    from test_telegram import OWNER

    from acp_gateway.connectors.protocol import ComputerDiagnostic, RuntimeResult

    _, channel, session, _ = telegram
    calls = []

    async def runtime(*args, **kwargs):
        calls.append(1)
        if case == "timeout":
            await asyncio.sleep(3)
        return RuntimeResult(
            page=1,
            pages=1,
            profiles=[],
            diagnostics=ComputerDiagnostic(
                version="0.2.8",
                updater="ready",
                maintenance="uncertain",
                running=0,
                requests=0,
                approvals=0,
                opening=0,
                probe="not_run",
            ),
        )

    channel.connector_dispatcher = SimpleNamespace(
        status=lambda: [
            {"computer_id": "work", "connected": True, "runtime_diagnostics": case != "old"}
        ],
        runtime=runtime,
    )
    row = {
        "role": "connector",
        "target": "work",
        "current_version": "0.2.7",
        "version": "0.2.8",
        "status": "failed",
        "id": "abc",
        "reason": "activity_uncertain",
    }
    await channel._update_operation_panel(OWNER, row)
    assert bool(calls) is (case != "old")
    if case == "new":
        assert "Текущее состояние: исход работы неизвестен" in session.sent[-1].text
    else:
        assert "Текущее состояние" not in session.sent[-1].text


async def test_unauthorized_chat_cannot_discover_versions_or_check_releases(telegram):
    _, channel, session, _ = telegram
    channel.update_inventory = UpdateInventory(
        package_lookup=lambda version: True,
        local_report=lambda: pytest.fail("unauthorized discovery"),
        release_lookup=lambda: pytest.fail("unauthorized lookup"),
    )
    await feed(channel, 1, "/updates_check", user=99999)
    assert not session.sent


async def test_release_error_and_old_offline_connectors_have_clear_status(telegram):
    _, channel, session, _ = telegram

    async def remote(target):
        return None

    def lookup():
        raise httpx.ConnectError("private diagnostic")

    dispatcher = SimpleNamespace(
        status=lambda: [
            {"computer_id": "home", "connected": False},
            {"computer_id": "work", "connected": True, "update_inventory": False},
        ],
        update_info=remote,
    )
    channel.update_inventory = UpdateInventory(
        dispatcher, package_lookup=lambda version: True, local_report=report, release_lookup=lookup
    )
    await feed(channel, 1, "/updates_check")
    text = session.sent[-1].text
    assert "не в сети" in text and "старому коннектору" in text
    assert "Не удалось проверить" in text and "private diagnostic" not in text
    await feed(channel, 2, "/updates -1")
    assert "Откройте /updates" in session.sent[-1].text


async def test_confirmed_update_buttons_are_private_and_admit_once(telegram, tmp_path, monkeypatch):
    import time

    from test_telegram import OTHER, OWNER

    from acp_gateway.update_journal import Journal
    from acp_gateway.update_service import LocalUpdates, UpdateService

    _, channel, session, server = telegram
    config = SimpleNamespace(
        settings=SimpleNamespace(
            updates=SimpleNamespace(enabled=True, spool_dir=tmp_path / "spool"),
            resolved_data_dir=lambda: tmp_path,
        )
    )
    local = LocalUpdates(config, role="dispatcher", target="dispatcher")
    monkeypatch.setattr("acp_gateway.update_service.installation_report", lambda **kw: report("uv"))
    spool = Journal(local.path)
    spool.set_worker(
        {"published": time.time(), "role": "dispatcher", "target": "dispatcher", "backend": "uv"}
    )
    journal = Journal(tmp_path / "control.db")
    channel.update_inventory = UpdateInventory(
        package_lookup=lambda version: True,
        local_report=local.report,
        release_lookup=lambda: Version("0.3.0"),
    )
    channel.update_service = UpdateService(channel.update_inventory, journal, local)
    try:
        await feed(channel, 1, "/updates_check")
        card = session.sent[-1]
        await click(channel, 2, card, button(card, "Обновить диспетчер").callback_data)
        confirmation = session.sent[-1]
        confirm = button(confirmation, "Подтвердить").callback_data
        assert "0.2.0 → 0.3.0" in confirmation.text
        assert "продолжение диалога" in confirmation.text
        assert "новая явная команда" in confirmation.text
        assert not spool.unfinished()
        await click(channel, 3, confirmation, confirm, user=OTHER)
        assert not spool.unfinished()
        await click(channel, 4, confirmation, confirm)
        assert len(spool.unfinished()) == 1
        await click(channel, 5, confirmation, confirm)
        assert len(spool.unfinished()) == 1
        row = journal.recent(actor=f"{channel.namespace}:{OWNER}")[0]
        spool.transition(row["id"], ("queued",), "succeeded")
        await feed(channel, 6, f"/update_status {row['id']}")
        assert "Обновление завершено" in session.sent[-1].text
        before = len(session.sent)
        await channel._update_notifications_once()
        await channel._update_notifications_once()
        assert len(session.sent) == before + 1
        assert journal.notice(row["id"], "succeeded") == "delivered"
        reopened = Journal(tmp_path / "control.db")
        assert reopened.notice(row["id"], "succeeded") == "delivered"
        reopened.close()
        await feed(channel, 7, f"/update_status {row['id']}", user=OTHER)
        assert "недоступна вашему чату" in session.sent[-1].text
        assert not server.log
        from aiogram.exceptions import TelegramNetworkError

        failed = journal.create(
            actor=f"{channel.namespace}:{OWNER}",
            role="dispatcher",
            target="dispatcher",
            version="0.4.0",
            current_version="0.3.0",
            binding="local",
            expires=time.time() + 60,
            status="failed",
        )
        session.failures.append(
            lambda method: TelegramNetworkError(method=method, message="ambiguous send")
        )
        with pytest.raises(TelegramNetworkError):
            await channel._update_notifications_once()
        assert journal.notice(failed["id"], "failed") == "unknown"
        before = len(session.sent)
        await channel._update_notifications_once()
        assert len(session.sent) == before
    finally:
        journal.close()
        spool.close()
