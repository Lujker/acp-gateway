# ruff: noqa: RUF001
"""Dispatcher dashboards: privacy, stable history and non-destructive navigation."""

import asyncio
import secrets
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText, SendMessage
from test_telegram import OTHER, OWNER, click, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.channels.telegram_ui import PANEL_CONTEXT
from acp_gateway.core import GatewayError
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, JobStatus


def jobs(core, key=OWNER, agent="retired", count=12, channel="telegram"):
    record = core.store.add_session(
        Conversation(channel, str(key), agent), f"{channel}-{key}-{agent}", "/work"
    )
    result = []
    for i in range(count):
        job = core.store.add_job(f"{channel}-{key}-{agent}-{i:03}", record.id)
        core.store.finish_job(job.id, JobStatus.COMPLETED, answer="private saved answer")
        result.append(job)
    return result


async def test_history_pagination_has_no_foreign_content_or_heavy_payload(telegram):
    core, channel, session, server = telegram
    own = jobs(core)
    foreign = jobs(core, key=OTHER)
    jobs(core, channel="cli")
    first = core.job_history("telegram", str(OWNER))
    assert len(first["jobs"]) == 10
    assert all(not hasattr(j, "answer") for j in first["jobs"])
    second = core.job_history("telegram", str(OWNER), before=first["next_before"])
    assert len(second["jobs"]) == 2
    assert not {j.id for j in first["jobs"]} & {j.id for j in second["jobs"]}
    assert second["next_before"] is None
    with pytest.raises(GatewayError):
        core.job_history("telegram", str(OWNER), before=foreign[0].id)
    await feed(channel, 1, "/jobs")
    card = session.sent[-1]
    assert "retired" in card.text and "private saved answer" not in card.text
    assert str(OTHER) not in card.text
    await click(channel, 2, card, button(card, "Еще").callback_data)
    assert len(session.sent) == 1
    assert own[0].id in session.sent[-1].text
    await click(channel, 3, card, button(card, "Еще").callback_data)
    assert session.calls[-1].show_alert
    assert not server.log


async def test_job_details_and_result_ignore_selected_route_but_enforce_chat(telegram):
    core, channel, session, _ = telegram
    own = jobs(core, count=1)[0]
    foreign = jobs(core, key=OTHER, count=1)[0]
    await feed(channel, 1, f"/job {own.id}")
    assert "retired" in session.sent[-1].text
    await feed(channel, 2, f"/delivery {own.id}")
    assert "доставки нет" in session.sent[-1].text
    await feed(channel, 3, f"/job {foreign.id}")
    assert "does not own" in session.sent[-1].text
    with pytest.raises(GatewayError):
        core.owned_job(foreign.id, channel="telegram", key=str(OWNER))


async def test_navigation_edits_menu_and_never_edits_answer(telegram):
    core, channel, session, server = telegram
    await feed(channel, 1, "/menu")
    home = session.sent[-1]
    await click(channel, 2, home, button(home, "Задачи").callback_data)
    assert len(session.sent) == 1 and "Задачи этого чата" in session.sent[-1].text
    assert any(isinstance(call, EditMessageText) for call in session.calls)
    job = jobs(core, count=1)[0]
    await channel._send_answer(OWNER, "**keep this answer**", core.job(job.id))
    answer = session.sent[-1]
    await click(channel, 3, answer, button(answer, "🖥").callback_data)
    assert session.sent[-2].text == answer.text
    assert session.sent[-1].message_id != answer.message_id
    assert not server.log


@pytest.mark.parametrize("missing", [True, False])
async def test_edit_rejection_only_falls_back_for_explicit_missing_message(telegram, missing):
    _, channel, session, _ = telegram
    await feed(channel, 1, "/menu")
    home = session.sent[-1]
    failure = TelegramBadRequest if missing else TelegramNetworkError
    session.edit_failures.append(
        lambda method: failure(
            method=method, message="message to edit not found" if missing else "ambiguous"
        )
    )
    await click(channel, 2, home, button(home, "Задачи").callback_data)
    assert len(session.sent) == (2 if missing else 1)
    assert sum(isinstance(c, SendMessage) for c in session.calls) == (2 if missing else 1)


async def test_late_panel_result_does_not_overwrite_newer_navigation(telegram):
    _, channel, session, _ = telegram
    await feed(channel, 1, "/menu")
    home = session.sent[-1]
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        token = PANEL_CONTEXT.set(channel._panels.begin_edit(home))
        try:
            started.set()
            await release.wait()
            await channel._panel(OWNER, "old response", channel._back_rows())
        finally:
            PANEL_CONTEXT.reset(token)

    task = asyncio.create_task(slow())
    await started.wait()
    await click(channel, 2, home, button(home, "Задачи").callback_data)
    release.set()
    await task
    assert "Задачи этого чата" in session.sent[-1].text
    assert len(session.sent) == 1


async def test_computers_are_snapshot_only_and_distinguish_disabled_and_offline(telegram):
    _, channel, session, server = telegram
    channel.connector_dispatcher = SimpleNamespace(
        status=lambda: [
            {
                "computer_id": "123",
                "display_name": "Home",
                "enabled": True,
                "connected": False,
                "advertised_agents": [],
            },
            {
                "computer_id": "work",
                "display_name": "Work",
                "enabled": False,
                "connected": False,
                "advertised_agents": [],
            },
        ]
    )
    await feed(channel, 1, "/computers")
    card = session.sent[-1]
    assert "Не подключен" in card.text and "Доступ выключен" in card.text
    await click(channel, 2, card, button(card, "Home").callback_data)
    assert "ID: 123" in session.sent[-1].text
    assert len(session.sent) == 1 and not server.log


async def test_ui_label_redacts_before_truncating_secret(telegram):
    from acp_gateway.channels.telegram import _label

    secret = secrets.token_hex(64)
    register_secret(secret)
    assert secret[:15] not in _label(secret, 15)


async def test_history_cursor_is_metadata_only_stable_and_scope_checked(telegram, monkeypatch):
    core, _, _, _ = telegram
    own = jobs(core, count=12)
    # Equal timestamps exercise the id tie breaker deterministically.
    core.store._conn.execute("UPDATE jobs SET created_at='2026-10-14T08:00:00+00:00'")
    first = core.job_history("telegram", str(OWNER), limit=5)
    record = core.store.session(own[0].session_id)
    core.store.add_job("newest", record.id)
    monkeypatch.setattr(core, "job", lambda _: pytest.fail("History loaded a full answer"))
    more = core.job_history("telegram", str(OWNER), limit=5, before=first["next_before"])
    assert not {j.id for j in first["jobs"]} & {j.id for j in more["jobs"]}
    assert "newest" not in {j.id for j in more["jobs"]}
    assert len(core.job_history("telegram", str(OWNER), active=True)["jobs"]) == 1
    with pytest.raises(GatewayError):
        core.job_history("telegram", str(OWNER), before=first["next_before"], agent="other")
    core.store._conn.execute("DELETE FROM jobs WHERE id=?", (first["next_before"],))
    with pytest.raises(GatewayError):
        core.job_history("telegram", str(OWNER), before=first["next_before"])


async def test_connected_computer_can_have_unchecked_agent_without_probe(telegram, monkeypatch):
    _, channel, session, server = telegram
    monkeypatch.setattr(
        "acp_gateway.channels.telegram_dashboard.computer_diagnostics",
        lambda *_: {
            "computers": [
                {"computer_id": "home", "display_name": "Home", "enabled": True, "connected": True}
            ],
            "routes": [
                {
                    "computer_id": "home",
                    "address": "home/hermes",
                    "alias": "hermes",
                    "status": "not_initialized",
                    "advertised": True,
                    "agent_ready": False,
                }
            ],
        },
    )
    channel.connector_dispatcher = object()
    await feed(channel, 1, "/computers id home")
    assert "готовность ещё не проверена" in session.sent[-1].text
    assert "Выбрать hermes" in button(session.sent[-1], "hermes").text
    assert not server.log
