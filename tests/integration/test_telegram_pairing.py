import asyncio
import secrets
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, User
from pydantic import SecretStr

from acp_gateway.channels.telegram import TelegramChannel
from acp_gateway.config import TelegramSettings
from acp_gateway.pairing import PairingService
from acp_gateway.storage.db import Store
from fakes.telegram import FakeTelegramSession


@pytest.fixture
def pairing_env(tmp_path):
    store = Store.open_in(tmp_path)
    now = [1000.0]
    pairing = PairingService(store, boundary=tmp_path / "boundary", clock=lambda: now[0])
    owner = pairing.bootstrap([123, 456])
    session = FakeTelegramSession()
    channel = TelegramChannel(
        TelegramSettings(allowed_user_ids=[123, 999]),
        SecretStr("42:fake"),
        bot=Bot("42:fake", session=session),
        pairing=pairing,
    )
    channel.core = SimpleNamespace(store=store)
    env = SimpleNamespace(
        store=store, now=now, pairing=pairing, owner=owner, session=session, channel=channel
    )
    yield env
    env.store.close()


async def test_automatic_owner_card_has_exact_target_comparison_and_no_secret(pairing_env):
    env = pairing_env
    secret = secrets.token_urlsafe(32)
    request = env.pairing.request(env.owner["owner_id"], secret, "<Work>", target="work")
    env.pairing.request("unknown", secrets.token_urlsafe(32), "Unknown")
    await env.channel._notify_pairing_once()
    assert len(env.session.sent) == 1
    card = env.session.sent[0]
    assert card.chat.id == 123
    assert request["request_id"] in card.text
    assert request["verification"] in card.text
    assert "work" in card.text and "&lt;Work&gt;" in card.text
    assert "Expires:" in card.text and "unsolicited" in card.text
    assert secret not in card.model_dump_json()
    buttons = card.reply_markup.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Подтвердить", "Отклонить"]
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert request["verification"] not in str(buttons)
    await env.channel._notify_pairing_once()
    # A replacement channel/service shares the durable receipts, not memory.
    env.channel.pairing = PairingService(
        env.store, boundary=env.pairing.boundary, clock=lambda: env.now[0]
    )
    await env.channel._notify_pairing_once()
    assert len(env.session.sent) == 1


def callback(card, *, index=0, user=123, data=None):
    return CallbackQuery(
        id="click",
        from_user=User(id=user, is_bot=False, first_name="Owner"),
        chat_instance="private",
        message=card,
        data=data or card.reply_markup.inline_keyboard[0][index].callback_data,
    )


@pytest.mark.parametrize("approve", [True, False])
async def test_inline_decision_is_exact_durable_and_cannot_reverse(pairing_env, tmp_path, approve):
    env = pairing_env
    env.store.computers.issue("work", tmp_path / "work.key", display_name="Work")
    env.store.computers.issue("other", tmp_path / "other.key", display_name="Other")
    other = (tmp_path / "other.key").read_text().strip()
    secret = secrets.token_urlsafe(32)
    req = env.pairing.request(env.owner["owner_id"], secret, "Replacement", target="work")
    await env.channel._notify_pairing_once()
    card = env.session.sent[0]
    env.store.close()
    env.store = Store.open_in(tmp_path)
    env.pairing = PairingService(
        env.store, boundary=tmp_path / "boundary", clock=lambda: env.now[0]
    )
    env.channel = TelegramChannel(
        env.channel.settings,
        SecretStr("42:fake"),
        bot=env.channel.bot,
        pairing=env.pairing,
    )
    env.channel.core = SimpleNamespace(store=env.store)
    query = callback(card, index=0 if approve else 1)
    await env.channel._callback(query)
    assert env.pairing.status(req["request_id"], secret)["status"] == (
        "approved" if approve else "denied"
    )
    await env.channel._callback(query)
    await env.channel._callback(callback(card, index=1 if approve else 0))
    decisions = [r for r in env.pairing.audit("local") if r["event"] in {"approved", "denied"}]
    assert len(decisions) == 1
    assert decisions[0]["actor"] == "telegram:123"
    if approve:
        issued = env.pairing.claim(req["request_id"], secret)
        assert issued["computer_id"] == "work"
        env.pairing.ack(req["request_id"], secret, issued["credential"])
    else:
        assert env.pairing.claim(req["request_id"], secret) == {"status": "denied"}
    assert env.store.computers.authenticate("other", other)


@pytest.mark.parametrize("error", [TelegramNetworkError, TelegramForbiddenError])
async def test_failed_notification_is_isolated_and_not_flooded(pairing_env, error):
    env = pairing_env
    secret = secrets.token_urlsafe(32)
    req = env.pairing.request(env.owner["owner_id"], secret, "PC")
    env.session.failures.append(lambda method: error(method=method, message="unavailable"))
    await env.channel._notify_pairing_once()
    assert env.pairing.status(req["request_id"], secret) == {"status": "pending"}
    await env.channel._notify_pairing_once()
    assert len([c for c in env.session.calls if isinstance(c, SendMessage)]) == 1
    # Explicit manual recovery remains available, and now has inline buttons.
    await env.channel._pairing_command(123, "/pairing", "")
    assert env.session.sent[-1].reply_markup is not None
    await env.channel._callback(callback(env.session.sent[-1], index=1))
    assert env.pairing.status(req["request_id"], secret) == {"status": "denied"}


async def test_explicit_telegram_backoff_is_durable_and_retried_only_when_due(pairing_env):
    env = pairing_env
    env.pairing.request(env.owner["owner_id"], secrets.token_urlsafe(32), "PC")
    env.session.failures.append(
        lambda method: TelegramRetryAfter(method=method, message="retry", retry_after=60)
    )
    await env.channel._notify_pairing_once()
    await env.channel._notify_pairing_once()
    assert len(env.session.calls) == 1
    env.channel.pairing = PairingService(
        env.store, boundary=env.pairing.boundary, clock=lambda: env.now[0]
    )
    env.now[0] += 60
    await env.channel._notify_pairing_once()
    assert len(env.session.sent) == 1


async def test_channel_lifecycle_runs_automatic_pairing_without_any_owner_command(pairing_env):
    from acp_gateway.core import GatewayCore

    env = pairing_env
    core = GatewayCore(env.store, {})
    env.pairing.request(env.owner["owner_id"], secrets.token_urlsafe(32), "PC")
    await env.channel.start(core)
    try:
        async with asyncio.timeout(2):
            while not env.session.sent:
                await asyncio.sleep(0.01)
        assert env.session.sent[0].reply_markup.inline_keyboard[0][0].callback_data.startswith("p:")
    finally:
        await env.channel.stop()
    assert not env.channel._tasks


@pytest.mark.parametrize(
    "tamper",
    [
        "nonowner",
        "other_owner",
        "group",
        "message",
        "namespace",
        "token",
        "action",
        "expired",
        "cancelled",
        "refreshed",
    ],
)
async def test_inline_cards_fail_closed_for_stale_or_foreign_context(pairing_env, tamper):
    env = pairing_env
    secret = secrets.token_urlsafe(32)
    req = env.pairing.request(env.owner["owner_id"], secret, "PC")
    await env.channel._notify_pairing_once()
    card = env.session.sent[0]
    query = callback(card)
    if tamper == "nonowner":
        forged = card.model_copy(update={"chat": Chat(id=999, type="private")})
        query = callback(forged, user=999)
    elif tamper == "other_owner":
        env.channel.settings.allowed_user_ids.append(456)
        forged = card.model_copy(update={"chat": Chat(id=456, type="private")})
        query = callback(forged, user=456)
    elif tamper == "group":
        query = callback(card.model_copy(update={"chat": Chat(id=123, type="group")}))
    elif tamper == "message":
        query = callback(card.model_copy(update={"message_id": card.message_id + 1}))
    elif tamper == "namespace":
        env.channel.namespace = "telegram:43"
    elif tamper == "token":
        query = callback(card, data="p:a:" + "0" * 32)
    elif tamper == "action":
        query = callback(card, data="p:yes:" + "0" * 32)
    elif tamper == "expired":
        env.now[0] += 601
    elif tamper == "cancelled":
        env.pairing.cancel(req["request_id"], secret)
    elif tamper == "refreshed":
        await env.channel._pairing_command(123, "/pairing", "")
    await env.channel._callback(query)
    assert not env.pairing.audit("local")
    assert not env.store.computers.list()


async def test_one_unreachable_owner_does_not_starve_another(pairing_env):
    env = pairing_env
    env.channel.settings.allowed_user_ids.append(456)
    req = env.pairing.request(env.owner["owner_id"], secrets.token_urlsafe(32), "PC")
    env.session.failures.append(lambda method: TelegramForbiddenError(method, "blocked"))
    await env.channel._notify_pairing_once()
    assert [card.chat.id for card in env.session.sent] == [456]
    assert req["request_id"] in env.session.sent[0].text
    await env.channel._notify_pairing_once()
    assert len(env.session.sent) == 1


async def test_running_channel_discovers_new_wire_request_despite_bot_outage(pairing_env):
    from acp_gateway.connectors.control import ControlDispatcher
    from acp_gateway.connectors.pairing_client import PairingClient
    from acp_gateway.core import GatewayCore

    env = pairing_env
    core = GatewayCore(env.store, {})
    dispatcher = ControlDispatcher(env.store.computers, pairing=env.pairing)
    env.session.failures.append(lambda method: TelegramNetworkError(method, "ambiguous"))
    await env.channel.start(core)
    try:
        async with dispatcher.listen("127.0.0.1", 0) as listener:
            url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/connect"
            client = PairingClient(env.pairing.boundary.parent / "applicant/pending.json")
            request = await client.begin(url, env.owner["owner_id"], "PC", allow_ws=True)
            assert request["state"] == "pending"
            async with asyncio.timeout(3):
                while not any(isinstance(call, SendMessage) for call in env.session.calls):
                    await asyncio.sleep(0.01)
            assert await client.finish() == {"status": "pending"}
            await env.channel._pairing_command(123, "/pairing", "")
            await env.channel._callback(callback(env.session.sent[-1]))
            saved = await client.finish()
            assert env.store.computers.list()[0]["computer_id"] == saved["computer_id"]
            assert env.store.computers.list()[0]["enabled"]
    finally:
        await env.channel.stop()
