import json

import pytest

from acp_gateway.connectors.pairing_client import PairingClient
from acp_gateway.pairing import PairingService
from acp_gateway.storage.db import Store


@pytest.mark.asyncio
async def test_client_saves_before_ack_and_recovers_lost_ack(tmp_path):
    from acp_gateway.connectors.pairing_client import PairingClient
    from acp_gateway.pairing import PairingService
    from acp_gateway.storage.db import Store

    store = Store.open_in(tmp_path / "server")
    service = PairingService(store, boundary=tmp_path / "server/pairing-boundary.json")
    owner = service.bootstrap([])
    client = PairingClient(tmp_path / "client/pending.json")
    lost = True

    async def rpc(url, payload, *, allow_ws):
        nonlocal lost
        action = payload["action"]
        args = {k: v for k, v in payload.items() if k != "action"}
        if action == "request":
            result = service.request(**args)
            service.decide("local", result["request_id"], result["verification"], approve=True)
            return result
        if action == "ack":
            state = json.loads(client.path.read_text())
            token = (client.path.parent / state["token_file"]).read_text().strip()
            assert token == payload["credential"]  # Durable save must precede activation.
            assert (
                state["issuance"]["computer_id"]
                == service.claim(args["request_id"], args["device_code"])["computer_id"]
            )
            service.ack(**args)
            if lost:
                lost = False
                raise OSError("lost ACK")
        return getattr(service, action)(**args)

    client.rpc = rpc
    await client.begin("ws://localhost:8766/connect", owner["owner_id"], "PC", allow_ws=True)
    with pytest.raises(OSError, match="lost ACK"):
        await client.finish()
    recovered = PairingClient(client.path)
    recovered.rpc = rpc
    result = await recovered.finish()
    assert result["computer_id"] == store.computers.list()[0]["computer_id"]
    assert len(store.computers.list()) == 1
    assert result["tls_fingerprint"] is None
    assert (client.path.stat().st_mode & 0o077) == 0
    store.close()


@pytest.mark.asyncio
async def test_plaintext_requires_consent_before_any_secret_or_file(tmp_path):
    from acp_gateway.connectors.pairing_client import PairingClient

    client = PairingClient(tmp_path / "pending.json")
    with pytest.raises(ValueError, match="consent"):
        await client.begin("ws://localhost/connect", "owner", "PC")
    assert not client.path.exists()


def test_completed_pairing_can_be_explicitly_archived_without_deleting_credential(tmp_path):
    import secrets

    from acp_gateway.connectors.pairing_client import PairingClient

    client = PairingClient(tmp_path / "pending.json")
    key = tmp_path / "device.key"
    key.write_text("keep old credential")
    client.save(
        {
            "version": 1,
            "device_code": secrets.token_urlsafe(32),
            "request": {"request_id": "a" * 32},
            "connection": {"token_file": str(key)},
        }
    )
    with pytest.raises(ValueError, match="explicit"):
        client.reset()
    client.reset(confirm=True)
    assert not client.path.exists()
    assert key.read_text() == "keep old credential"
    assert (tmp_path / ("a" * 32 + ".completed.json")).exists()


@pytest.mark.asyncio
async def test_concurrent_local_wizard_cannot_replace_applicant_state(tmp_path):
    import asyncio

    from acp_gateway.connectors.pairing_client import PairingClient

    first = PairingClient(tmp_path / "pending.json")
    second = PairingClient(first.path)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def rpc(url, payload, **kwargs):
        calls.append(payload["device_code"])
        if len(calls) > 1:
            return {"request_id": "b" * 32}
        started.set()
        await release.wait()
        return {"request_id": "a" * 32, "verification": "A" * 24, "target": None, "expires": 1000}

    first.rpc = second.rpc = rpc
    task = asyncio.create_task(first.begin("wss://server/connect", "owner", "PC"))
    await started.wait()
    try:
        with pytest.raises(ValueError, match="another pairing"):
            await second.begin("wss://server/connect", "owner", "PC")
    finally:
        release.set()
        await task
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lost_ack_recovers_after_request_expiry_and_sweeper(tmp_path):
    now = [1000.0]
    store = Store.open_in(tmp_path / "server")
    service = PairingService(
        store,
        boundary=tmp_path / "server/pairing-boundary.json",
        clock=lambda: now[0],
        ttl=60,
    )
    owner = service.bootstrap([])
    client = PairingClient(tmp_path / "client/pending.json")
    lost = True

    async def rpc(url, payload, *, allow_ws):
        nonlocal lost
        action = payload["action"]
        args = {key: value for key, value in payload.items() if key != "action"}
        if action == "request":
            result = service.request(**args)
            service.decide("local", result["request_id"], result["verification"], approve=True)
            return result
        if action == "ack":
            state = json.loads(client.path.read_text())
            token = (client.path.parent / state["token_file"]).read_text().strip()
            assert token == payload["credential"]
            service.ack(**args)
            if lost:
                lost = False
                raise OSError("lost ACK")
        return getattr(service, action)(**args)

    client.rpc = rpc
    await client.begin("ws://localhost:8766/connect", owner["owner_id"], "PC", allow_ws=True)
    with pytest.raises(OSError, match="lost ACK"):
        await client.finish()

    now[0] += 61
    service.cleanup()
    recovered = PairingClient(client.path)
    recovered.rpc = rpc
    result = await recovered.finish()
    assert result["computer_id"] == store.computers.list()[0]["computer_id"]
    assert len(store.computers.list()) == 1
    store.close()
