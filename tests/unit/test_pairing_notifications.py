import secrets

import pytest

from acp_gateway.pairing import PairingService
from acp_gateway.storage.db import Store


def test_notification_claim_is_owner_only_durable_and_never_contains_credentials(tmp_path):
    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "boundary")
    owner = pairing.bootstrap([123])
    secret = secrets.token_urlsafe(32)
    request = pairing.request(owner["owner_id"], secret, "Work", target="work")
    with pytest.raises(ValueError, match="authorization"):
        pairing.claim_notification("telegram:999", "telegram:42", request["request_id"])
    card = pairing.claim_notification("telegram:123", "telegram:42", request["request_id"])
    assert card["target"] == "work"
    assert card["verification"] == request["verification"]
    assert secret not in str(card)
    assert not {"applicant_digest", "credential", "device_code"}.intersection(card)
    assert pairing.claim_notification("telegram:123", "telegram:42", request["request_id"]) is None
    store.close()
    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "boundary")
    assert pairing.claim_notification("telegram:123", "telegram:42", request["request_id"]) is None
    store.close()
