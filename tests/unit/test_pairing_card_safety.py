import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from acp_gateway.pairing import PairingService
from acp_gateway.storage.db import Store


def test_reject_changed_target_is_safe_without_rotating_or_enabling_it(tmp_path):
    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "boundary")
    owner = pairing.bootstrap([123])
    store.computers.issue("work", tmp_path / "key", display_name="Work")
    secret = secrets.token_urlsafe(32)
    request = pairing.request(owner["owner_id"], secret, "Replacement", target="work")
    card = pairing.claim_notification("telegram:123", "telegram:42", request["request_id"])
    pairing.settle_notification(
        "telegram:123", "telegram:42", card["token"], "delivered", message_id=7
    )
    store.computers.revoke("work")
    with pytest.raises(ValueError, match="changed"):
        pairing.decide_notification("telegram:123", "telegram:42", card["token"], 7, approve=True)
    assert (
        pairing.decide_notification("telegram:123", "telegram:42", card["token"], 7, approve=False)[
            "state"
        ]
        == "denied"
    )
    assert pairing.claim(request["request_id"], secret) == {"status": "denied"}
    assert not store.computers.list()[0]["enabled"]
    store.close()


def test_concurrent_notification_claim_and_opposing_decisions_are_serialized(tmp_path):
    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "boundary")
    owner = pairing.bootstrap([123])
    req = pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "PC")
    barrier = Barrier(4)

    def attempt(index):
        db = Store.open_in(tmp_path)
        app = PairingService(db, boundary=tmp_path / "boundary")
        try:
            barrier.wait(timeout=5)
            if index < 0:
                try:
                    return app.decide_notification(
                        "telegram:123", "telegram:42", token, 7, approve=index == -1
                    )["state"]
                except ValueError:
                    return "resolved"
            return app.claim_notification("telegram:123", "telegram:42", req["request_id"])
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    (card,) = [result for result in results if result is not None]
    token = card["token"]
    pairing.settle_notification("telegram:123", "telegram:42", token, "delivered", message_id=7)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, [-1, -2, -1, -2]))
    state = pairing.conn.execute("SELECT state FROM pairing_requests").fetchone()[0]
    assert state in {"approved", "denied"}
    assert set(results) <= {state, "resolved"}
    assert len(pairing.audit("local")) == 1
    store.close()
