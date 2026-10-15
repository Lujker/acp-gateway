# ruff: noqa: RUF001
"""Error recovery is explicit navigation; failures never retry agent work."""

import secrets

import pytest
from test_telegram import OWNER, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.agents import (
    AgentUnavailable,
    AuthenticationFailed,
    SessionBusy,
    SessionNotFound,
    TLSFingerprintMismatch,
    TransportDisconnected,
)
from acp_gateway.core import (
    JobNotFound,
    MaintenanceBlocked,
    RateLimited,
    SelectionChanged,
    UnknownAgent,
    UnknownSession,
)
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation


@pytest.mark.parametrize(
    "error,explanation,action",
    [
        (AgentUnavailable("offline"), "Агент недоступен", "Компьютеры"),
        (TransportDisconnected("lost"), "Не отправляйте запрос повторно", "История задач"),
        (AuthenticationFailed("access"), "Подключение отклонено", "Компьютеры"),
        (TLSFingerprintMismatch("pin"), "Подключение отклонено", "Компьютеры"),
        (SessionNotFound("lost dialogue"), "Диалог не выбран или недоступен", "Диалоги"),
        (UnknownSession("no dialogue"), "Диалог не выбран или недоступен", "Диалоги"),
        (SessionBusy("unknown operation"), "требует проверки", "Активные задачи"),
        (UnknownAgent("retired route"), "Агент не выбран", "Выбрать агента"),
        (JobNotFound("foreign job"), "Задача недоступна этому чату", "История задач"),
        (RateLimited(12), "Подождите 12 с", "Активные задачи"),
        (MaintenanceBlocked("paused"), "временно не принимает", "Статус"),
        (SelectionChanged("changed"), "Выбор агента или диалога изменился", "Обновить панель"),
    ],
)
async def test_error_card_has_a_next_action_without_replaying_work(
    telegram, monkeypatch, error, explanation, action
):
    core, channel, session, server = telegram
    core.store.add_session(Conversation("telegram", str(OWNER), "work"), "error", "/work")
    calls = []

    async def rejected(*args, **kwargs):
        calls.append(args)
        raise error

    monkeypatch.setattr(core, "submit_selected", rejected)
    await feed(channel, 1, "do not replay")
    card = session.sent[-1]
    assert explanation in card.text and str(error) in card.text
    assert button(card, action)
    assert len(calls) == 1 and not server.log
    assert not core.job_history("telegram", str(OWNER))["jobs"]


async def test_error_diagnostics_are_redacted_and_html_escaped(telegram, monkeypatch):
    core, channel, session, _ = telegram
    core.store.add_session(Conversation("telegram", str(OWNER), "work"), "redact", "/work")
    secret = secrets.token_hex(32)
    register_secret(secret)

    async def rejected(*args, **kwargs):
        raise AgentUnavailable("<address> " + secret)

    monkeypatch.setattr(core, "submit_selected", rejected)
    await feed(channel, 1, "do not replay")
    assert secret not in session.sent[-1].text
    assert "&lt;address&gt;" in session.sent[-1].text


async def test_help_explains_plain_text_rename_cancel_and_saved_result(telegram):
    _, channel, session, server = telegram
    await feed(channel, 1, "/help")
    text = session.sent[-1].text
    assert "/cancel" in text
    assert "затем отправьте название" in text and "без запуска агента" in text
    assert "Обычные сообщения идут в выбранный диалог" in text
    assert not server.log
