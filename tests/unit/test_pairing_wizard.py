import json
import secrets
from unittest.mock import AsyncMock

import pytest
from tests.fakes.setup_prompts import Answers

from acp_gateway.cli import wizard as w
from acp_gateway.connectors.pairing_client import PairingClient


def test_connector_connection_ordinary_flow_never_asks_for_key_or_yaml(
    tmp_path, monkeypatch, capsys
):
    from acp_gateway.connectors.pairing_client import PairingClient

    async def begin(self, url, owner_id, name, **kwargs):
        assert url == "wss://dispatcher.example:8766"
        assert owner_id == "owner-example"
        return {
            "request_id": "abc",
            "verification": "COMPARE-EXACT",
            "target": None,
            "expires": 1000,
        }

    async def finish(self):
        return {
            "dispatcher_url": "wss://dispatcher.example:8766/connect",
            "computer_id": "pc_new",
            "token_file": str(tmp_path / "automatic.key"),
            "tls_fingerprint": None,
        }

    monkeypatch.setattr(PairingClient, "begin", begin)
    monkeypatch.setattr(PairingClient, "finish", finish)

    class Prompt(Answers):
        def ask(self, label, *args, **kwargs):
            assert "key file" not in label and "fingerprint" not in label and "pin" not in label
            return super().ask(label, *args, **kwargs)

    prompts = Prompt(
        **{
            "Server connection URL": "dispatcher.example:8766",
            "Dispatcher owner ID": "owner-example",
            "Owner approved this exact request? Check now": True,
        }
    )
    connection = w._connection(prompts, {}, pairing_path=tmp_path / "pending.json")
    assert connection["computer_id"] == "pc_new"
    assert "COMPARE-EXACT" in capsys.readouterr().out


def test_rejected_ws_does_not_start_pairing(tmp_path, monkeypatch):
    from acp_gateway.connectors.pairing_client import PairingClient

    begin = AsyncMock()
    monkeypatch.setattr(PairingClient, "begin", begin)
    with pytest.raises(ValueError, match="WSS"):
        w._connection(
            Answers(**{"Server connection URL": "ws://dispatcher:8766"}),
            {},
            pairing_path=tmp_path / "pending.json",
        )
    begin.assert_not_awaited()


def test_connect_on_fresh_pc_opens_full_connector_setup(tmp_path, monkeypatch):
    import sys

    from acp_gateway.cli.main import main

    class TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", TTY())
    monkeypatch.setattr(sys, "stdout", TTY())
    calls = []
    monkeypatch.setattr(w, "wizard", lambda **kwargs: calls.append(kwargs) or 0)
    assert main(["--config", str(tmp_path / "new.yaml"), "connect"]) == 0
    assert calls[0]["section"] == "all"
    assert calls[0]["role"] == "connector"


def test_server_setup_bootstraps_only_explicit_pairing_approvers(tmp_path, monkeypatch, capsys):
    from tests.fakes.setup_prompts import save_answers

    from acp_gateway.pairing import PairingService
    from acp_gateway.storage.db import Store

    monkeypatch.setattr(w, "_managed_running", lambda *a: False)
    monkeypatch.setattr(w, "_check_ports", lambda *a: None)
    monkeypatch.setattr(w.paths, "data_dir", lambda: tmp_path / "data")
    assert (
        w.wizard(
            tmp_path,
            role="server",
            prompts=save_answers(
                **{
                    "Bootstrap connector pairing owner now?": True,
                    "Telegram pairing approver IDs (comma-separated; empty means "
                    "local CLI only)": "123",
                }
            ),
        )
        == 0
    )
    store = Store.open_in(tmp_path / "data")
    pairing = PairingService(store, boundary=tmp_path / "data/pairing-boundary.json")
    assert pairing.owner("telegram:123")["owner_id"] in capsys.readouterr().out
    with pytest.raises(ValueError, match="authorization"):
        pairing.owner("telegram:999")
    store.close()


def test_unacknowledged_issuance_is_resumed_instead_of_replaced(tmp_path, monkeypatch):
    from tests.fakes.setup_prompts import Answers

    from acp_gateway.cli import wizard as w

    path = tmp_path / ".config.yaml.pairing"
    PairingClient(path).save(
        {
            "version": 1,
            "input_url": "wss://dispatcher.example/connect",
            "url": "wss://dispatcher.example/connect",
            "owner_id": "owner-example",
            "name": "PC",
            "target": None,
            "allow_ws": False,
            "device_code": secrets.token_urlsafe(32),
            "request": {
                "request_id": "a" * 32,
                "verification": "0" * 24,
                "target": None,
                "expires": 1,
            },
            "issuance": {"computer_id": "pc_issued", "token_file": "a.key"},
        }
    )
    actions = []

    class Prompt(Answers):
        def choice(self, label, choices, default=None):
            actions.append((label, tuple(choices)))
            return "resume"

    async def begin(self, *args, **kwargs):
        return json.loads(self.path.read_text())["request"]

    async def finish(self):
        return {
            "dispatcher_url": "wss://dispatcher.example/connect",
            "computer_id": "pc_issued",
            "token_file": str(path.parent / "a.key"),
            "tls_fingerprint": None,
        }

    monkeypatch.setattr(PairingClient, "begin", begin)
    monkeypatch.setattr(PairingClient, "finish", finish)
    prompts = Prompt(
        **{
            "Dispatcher owner ID": "owner-example",
            "Computer display name": "PC",
            "Owner approved this exact request? Check now": True,
        }
    )
    result = w._connection(prompts, {}, pairing_path=path)
    assert result["computer_id"] == "pc_issued"
    # The resume/start-another choice is not offered while an issuance is pending.
    assert all("start another" not in choices for _, choices in actions)
