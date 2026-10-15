# ruff: noqa: RUF001
"""Observed stages and model evidence never imply an automatic model retry."""

import pytest
from aiogram.methods import SendMessage
from test_telegram import OWNER, feed
from test_telegram import telegram as telegram
from test_telegram_controls import button

from acp_gateway.agents import ModeNotAvailable
from acp_gateway.agents.errors import ModelRefused
from acp_gateway.storage import Conversation


async def test_initial_work_card_acknowledges_admission_without_inventing_a_queue(telegram):
    core, channel, session, _ = telegram
    await feed(channel, 1, "/new")
    await feed(channel, 2, "say pong")
    messages = [m.text for m in session.calls if isinstance(m, SendMessage)]
    assert any("Принято диспетчером" in text for text in messages)
    assert not any("Очередь" in text for text in messages)
    assert len(core.job_history("telegram", str(OWNER))["jobs"]) == 1


async def test_unknown_model_state_is_read_only_and_explicit_reconcile_never_retries(telegram):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "model-unknown", "/work")
    rev = core.store.active_revision(conv)
    core.store.models.begin(record.id, "provider", "model", rev)
    core.store.models.settle(record.id, "unknown")
    before = core.store.models.state(record.id)
    await feed(channel, 1, "/model state")
    card = session.sent[-1]
    assert "Исход смены модели неизвестен" in card.text
    assert "без подключения" in card.text and "requested_model" in card.text
    from test_telegram import click

    await click(channel, 2, card, button(card, "Проверить сохранённый исход").callback_data)
    assert "Проверка не подтвердила итог" in next(
        m.text for m in session.sent if m.message_id == card.message_id
    )
    assert core.store.models.state(record.id) == before and not server.log


@pytest.mark.parametrize(
    "state,text",
    [
        ("pending", "применение ещё не подтверждено"),
        ("refused", "Смена модели отклонена"),
        ("confirmation_required", "требует вашего явного подтверждения"),
    ],
)
async def test_saved_model_outcomes_are_not_reported_as_applied(telegram, state, text):
    core, channel, session, server = telegram
    conv = Conversation("telegram", str(OWNER), "work")
    record = core.store.add_session(conv, "model-state", "/work")
    core.store.models.begin(record.id, "provider", "model", core.store.active_revision(conv))
    core.store.models.settle(record.id, state)
    await feed(channel, 1, "/model state")
    assert text in session.sent[-1].text and not server.log


@pytest.mark.parametrize(
    "error,expected,label",
    [
        (ModelRefused("refused"), "отклонил смену модели", "Сведения о модели"),
        (ModeNotAvailable("host mode"), "не поддерживает настроенный режим", "Выбрать агента"),
    ],
)
async def test_special_errors_offer_actions_without_authorizing_another_mode(
    telegram, monkeypatch, error, expected, label
):
    core, channel, session, server = telegram
    core.store.add_session(Conversation("telegram", str(OWNER), "work"), "typed-error", "/work")

    async def rejected(*args, **kwargs):
        raise error

    monkeypatch.setattr(core, "submit_selected", rejected)
    await feed(channel, 1, "do not retry")
    assert expected in session.sent[-1].text
    assert button(session.sent[-1], label)
    assert not any(
        "Без подтверждений" in b.text
        for row in session.sent[-1].reply_markup.inline_keyboard
        for b in row
    )
    assert not server.log
