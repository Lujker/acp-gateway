import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from acp_gateway.cli.main import main
from acp_gateway.config import AppConfig, SecretStore, Settings, TelegramSettings
from acp_gateway.storage.db import Store


def test_local_owner_bootstrap_and_decision_cli(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path.as_posix()}\n")
    args = ["--config", str(config)]
    assert main([*args, "owner", "bootstrap", "--telegram-id", "123"]) == 0
    import json

    owner = json.loads(capsys.readouterr().out)
    from acp_gateway.pairing import PairingService

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    req = pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "Laptop")
    assert (
        main(
            [*args, "pairing", "approve", req["request_id"], "--verification", req["verification"]]
        )
        == 0
    )
    assert pairing.pending("local") == []
    store.close()


@pytest.mark.asyncio
async def test_telegram_approver_is_explicit_not_entire_allowlist(tmp_path):
    from acp_gateway.channels.telegram import TelegramChannel
    from acp_gateway.pairing import PairingService

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    owner = pairing.bootstrap([123])
    req = pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "Laptop", target=None)
    channel = TelegramChannel(
        TelegramSettings(allowed_user_ids=[123, 999]), SecretStr("123:fake"), pairing=pairing
    )
    channel._send = AsyncMock(return_value=SimpleNamespace(message_id=1))
    assert await channel._pairing_command(123, "/pairing", "")
    assert req["verification"] in channel._send.call_args.args[1]
    assert await channel._pairing_command(
        999, "/pair_approve", req["request_id"] + " " + req["verification"]
    )
    assert len(pairing.pending("local")) == 1
    assert await channel._pairing_command(
        123, "/pair_approve", req["request_id"] + " " + req["verification"]
    )
    assert pairing.pending("local") == []
    await channel.bot.session.close()
    store.close()


def test_configured_dispatcher_exposes_pairing_without_bootstrapping_approvers(tmp_path):
    from acp_gateway.daemon import configured_app

    settings = Settings.model_validate(
        {"data_dir": str(tmp_path), "connector": {"enabled": True}, "pairing": {"max_requests": 8}}
    )
    cfg = AppConfig(settings, SecretStore({"ACPGW_API_TOKEN": "owner-secret"}), None, None)
    with configured_app(cfg) as app:
        pairing = app.state.dispatcher.pairing.service
        assert pairing.capacity == 8
        with pytest.raises(ValueError, match="authorization"):
            pairing.owner("telegram:123")
        assert not (tmp_path / "pairing-boundary.json").exists()


@pytest.mark.asyncio
async def test_idle_pairing_sweeper_removes_expired_secrets(tmp_path):
    import asyncio

    from acp_gateway.daemon import sweep_pairing
    from acp_gateway.pairing import PairingService

    now = [1000.0]
    store = Store.open_in(tmp_path)
    pairing = PairingService(
        store, boundary=tmp_path / "pairing-boundary.json", clock=lambda: now[0]
    )
    owner = pairing.bootstrap([])
    pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "PC")
    now[0] += 601
    task = asyncio.create_task(sweep_pairing(pairing, interval=0.01))
    try:
        await asyncio.sleep(0.03)
        assert store._conn.execute("SELECT count(*) FROM pairing_requests").fetchone()[0] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


def test_pairing_and_owner_are_discoverable_in_telegram_menu():
    from acp_gateway.channels.telegram import COMMANDS

    assert {"owner", "pairing"}.issubset({row[0] for row in COMMANDS})
