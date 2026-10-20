"""Pairing lifecycle uses real SQLite and isolated files, never live accounts."""

import secrets

import pytest

from acp_gateway.storage.db import Store


def service(tmp_path, **kwargs):
    from acp_gateway.pairing import PairingService

    store = Store.open_in(tmp_path)
    return store, PairingService(store, boundary=tmp_path / "pairing-boundary.json", **kwargs)


def test_owner_approved_credential_stays_inactive_until_ack_and_recovers(tmp_path):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([123])
    secret = secrets.token_urlsafe(32)
    request = pairing.request(owner["owner_id"], secret, "Laptop")
    assert "device_code" not in request
    pairing.decide("local", request["request_id"], request["verification"], approve=True)
    claimed = pairing.claim(request["request_id"], secret)
    assert claimed["status"] == "issued"
    assert not store.computers.authenticate(claimed["computer_id"], claimed["credential"])
    assert pairing.claim(request["request_id"], secret) == claimed
    assert pairing.ack(request["request_id"], secret, claimed["credential"])["status"] == "active"
    assert store.computers.authenticate(claimed["computer_id"], claimed["credential"])
    store.close()
    store, pairing = service(tmp_path)
    assert pairing.claim(request["request_id"], secret) == {**claimed, "status": "active"}
    assert pairing.ack(request["request_id"], secret, claimed["credential"])["status"] == "active"
    assert len(store.computers.list()) == 1
    store.close()


def test_revoke_fences_unacknowledged_issuance(tmp_path):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([])
    code = secrets.token_urlsafe(32)
    req = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("local", req["request_id"], req["verification"], approve=True)
    issued = pairing.claim(req["request_id"], code)
    store.computers.revoke(issued["computer_id"])
    with pytest.raises(ValueError, match="access changed"):
        pairing.claim(req["request_id"], code)
    with pytest.raises(ValueError, match="access changed"):
        pairing.ack(req["request_id"], code, issued["credential"])
    assert not store.computers.authenticate(issued["computer_id"], issued["credential"])
    store.close()


def test_migration_requires_explicit_activation_fences_and_refuses_snapshot_rollback(tmp_path):
    import shutil

    store, pairing = service(tmp_path)
    pairing.bootstrap([])
    store.computers.issue("old", tmp_path / "old.key", display_name="Old PC")
    old = (tmp_path / "old.key").read_text().strip()
    grant = store.computers.authorize("old", old)
    assert grant is not None  # Merely loading new software does not activate migration.
    with pytest.raises(ValueError, match="explicit"):
        pairing.activate_migration("local", confirm=False)
    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copyfile(tmp_path / "gateway.db", tmp_path / "before.db")
    pairing.activate_migration("local", confirm=True)
    assert not store.computers.authenticate("old", old)
    assert not store.computers.grant_valid("old", grant)
    assert store.computers.list()[0]["display_name"] == "Old PC"
    store.close()
    for suffix in ("-wal", "-shm"):
        (tmp_path / ("gateway.db" + suffix)).unlink(missing_ok=True)
    shutil.copyfile(tmp_path / "before.db", tmp_path / "gateway.db")
    store = Store.open_in(tmp_path)
    assert not store.computers.authenticate("old", old)
    from acp_gateway.pairing import PairingService

    with pytest.raises(ValueError, match="rollback"):
        PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    store.close()


def test_cancel_and_read_only_status_bound_to_applicant(tmp_path):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([])
    secret = secrets.token_urlsafe(32)
    req = pairing.request(owner["owner_id"], secret, "PC")
    assert pairing.status(req["request_id"], secret) == {"status": "pending"}
    assert pairing.cancel(req["request_id"], secret) == {"status": "cancelled"}
    assert pairing.claim(req["request_id"], secret) == {"status": "cancelled"}
    assert pairing.pending("local") == []
    store.close()


def test_duplicate_approval_after_activation_is_idempotent_and_audited(tmp_path):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([123])
    code = secrets.token_urlsafe(32)
    req = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("telegram:123", req["request_id"], req["verification"], approve=True)
    issued = pairing.claim(req["request_id"], code)
    pairing.ack(req["request_id"], code, issued["credential"])
    assert (
        pairing.decide("telegram:123", req["request_id"], req["verification"], approve=True)[
            "state"
        ]
        == "active"
    )
    audit = pairing.audit("local")
    assert [event["event"] for event in audit] == ["approved", "issued", "active"]
    assert all(code not in str(event) and issued["credential"] not in str(event) for event in audit)
    store.close()


def test_missing_external_boundary_is_fail_closed(tmp_path):
    store, pairing = service(tmp_path)
    pairing.bootstrap([])
    store.computers.issue("old", tmp_path / "key", display_name="PC")
    pairing.activate_migration("local", confirm=True)
    (tmp_path / "pairing-boundary.json").unlink()
    from acp_gateway.pairing import PairingService

    with pytest.raises(ValueError, match="boundary"):
        PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    store.close()


def test_untrusted_applicant_secrets_do_not_grow_global_logging_registry(tmp_path):
    from acp_gateway import log

    store, pairing = service(tmp_path)
    before = set(log._registry)
    pairing.request("unknown", secrets.token_urlsafe(32), "PC")
    assert set(log._registry) == before
    store.close()


def test_concurrent_claim_is_one_exact_issuance(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([])
    code = secrets.token_urlsafe(32)
    req = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("local", req["request_id"], req["verification"], approve=True)
    gate = Barrier(6)

    def claim(_):
        local, app = service(tmp_path)
        try:
            gate.wait(timeout=10)
            return app.claim(req["request_id"], code)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert all(value == results[0] for value in results)
    assert len(store.computers.list()) == 1
    store.close()


@pytest.mark.parametrize(
    "failure",
    [
        "actor",
        "comparison",
        "foreign_owner",
        "applicant",
        "denied",
        "expired_approved",
        "target_revision",
    ],
)
def test_security_negative_paths_do_not_issue_access(tmp_path, failure):
    now = [1000.0]
    store, pairing = service(tmp_path, clock=lambda: now[0])
    owner = pairing.bootstrap([123])
    secret = secrets.token_urlsafe(32)
    target = None
    if failure == "target_revision":
        store.computers.issue("old", tmp_path / "key", display_name="Old")
        target = "old"
    req = pairing.request(
        "foreign" if failure == "foreign_owner" else owner["owner_id"], secret, "PC", target
    )
    if failure in {"actor", "comparison", "foreign_owner"}:
        with pytest.raises(ValueError):
            pairing.decide(
                "telegram:999" if failure == "actor" else "local",
                req["request_id"],
                "0" * 24 if failure == "comparison" else req["verification"],
                approve=True,
            )
    elif failure == "applicant":
        pairing.decide("local", req["request_id"], req["verification"], approve=True)
        with pytest.raises(ValueError):
            pairing.claim(req["request_id"], secrets.token_urlsafe(32))
    elif failure == "denied":
        pairing.decide("local", req["request_id"], req["verification"], approve=False)
        assert pairing.claim(req["request_id"], secret) == {"status": "denied"}
    elif failure == "expired_approved":
        pairing.decide("local", req["request_id"], req["verification"], approve=True)
        now[0] += 601
        with pytest.raises(ValueError, match="expired"):
            pairing.claim(req["request_id"], secret)
    else:
        pairing.decide("local", req["request_id"], req["verification"], approve=True)
        store.computers.revoke("old")
        with pytest.raises(ValueError, match="target changed"):
            pairing.claim(req["request_id"], secret)
    assert all(not r["computer_id"].startswith("pc_") for r in store.computers.list())
    store.close()


def test_unknown_owner_requests_share_capacity_cleanup_and_public_shape(tmp_path):
    now = [1000.0]
    store, pairing = service(tmp_path, clock=lambda: now[0], capacity=2)
    owner = pairing.bootstrap([])
    known = pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "PC")
    dummy = pairing.request("unknown", secrets.token_urlsafe(32), "PC")
    assert known.keys() == dummy.keys()
    assert known["state"] == dummy["state"] == "pending"
    with pytest.raises(ValueError, match="capacity"):
        pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "PC")
    now[0] += 601
    pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "PC")
    assert store._conn.execute("SELECT count(*) FROM pairing_requests").fetchone()[0] == 1
    store.close()


def test_owner_cannot_rebind_a_computer_bound_to_foreign_owner(tmp_path):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([])
    store.computers.issue("other", tmp_path / "key", display_name="Other owner")
    store._conn.execute("INSERT INTO pairing_bindings VALUES ('other', 'foreign-owner', 1)")
    req = pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "Pretend", target="other")
    with pytest.raises(ValueError, match="owner"):
        pairing.decide("local", req["request_id"], req["verification"], approve=True)
    assert store.computers.authenticate("other", (tmp_path / "key").read_text().strip())
    store.close()


@pytest.mark.parametrize("comparison", [None, "é" * 24, "A" * 10000, "wrong"])
def test_malformed_comparison_rejected_without_adapter_crash(tmp_path, comparison):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([])
    req = pairing.request(owner["owner_id"], secrets.token_urlsafe(32), "PC")
    with pytest.raises(ValueError, match="comparison"):
        pairing.decide("local", req["request_id"], comparison, approve=True)
    store.close()


def test_changed_binding_owner_fences_ack(tmp_path):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([])
    code = secrets.token_urlsafe(32)
    req = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("local", req["request_id"], req["verification"], approve=True)
    issued = pairing.claim(req["request_id"], code)
    store._conn.execute("UPDATE pairing_bindings SET owner_id='other'")
    with pytest.raises(ValueError, match="access changed"):
        pairing.ack(req["request_id"], code, issued["credential"])
    assert not store.computers.authenticate(issued["computer_id"], issued["credential"])
    store.close()


def test_runtime_boundary_change_fences_unacknowledged_credential(tmp_path):
    store, pairing = service(tmp_path)
    owner = pairing.bootstrap([])
    code = secrets.token_urlsafe(32)
    req = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("local", req["request_id"], req["verification"], approve=True)
    issued = pairing.claim(req["request_id"], code)
    pairing.activate_migration("local", confirm=True)
    (tmp_path / "pairing-boundary.json").unlink()
    with pytest.raises(ValueError, match="access changed"):
        pairing.ack(req["request_id"], code, issued["credential"])
    store.close()


"""Regression tests for an issued but unacknowledged credential."""


def test_issued_credential_survives_expiry_and_cleanup_until_ack(tmp_path):
    from acp_gateway.pairing import PairingService
    from acp_gateway.storage.db import Store

    now = [1000.0]
    store = Store.open_in(tmp_path)
    pairing = PairingService(
        store, boundary=tmp_path / "pairing-boundary.json", clock=lambda: now[0], ttl=60
    )
    owner = pairing.bootstrap([])
    code = secrets.token_urlsafe(32)
    request = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("local", request["request_id"], request["verification"], approve=True)
    issued = pairing.claim(request["request_id"], code)

    now[0] += 61
    pairing.cleanup()  # The periodic sweeper must not erase an unacknowledged issuance.
    assert pairing.claim(request["request_id"], code) == issued
    assert pairing.status(request["request_id"], code)["status"] == "issued"
    assert pairing.ack(request["request_id"], code, issued["credential"])["status"] == "active"
    assert store.computers.authenticate(issued["computer_id"], issued["credential"])
    store.close()


def test_a_denied_request_is_terminal_and_can_be_cancelled_locally(tmp_path):
    from acp_gateway.pairing import PairingService
    from acp_gateway.storage.db import Store

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    owner = pairing.bootstrap([])
    code = secrets.token_urlsafe(32)
    request = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("local", request["request_id"], request["verification"], approve=False)
    assert pairing.cancel(request["request_id"], code)["status"] == "cancelled"
    assert pairing.claim(request["request_id"], code)["status"] == "cancelled"
    store.close()


"""Rotation/enrollment after migration."""


def test_rotation_after_migration_keeps_the_pairing_binding_valid(tmp_path):
    from acp_gateway.pairing import PairingService
    from acp_gateway.storage.db import Store

    store = Store.open_in(tmp_path)
    pairing = PairingService(store, boundary=tmp_path / "pairing-boundary.json")
    owner = pairing.bootstrap([])
    code = secrets.token_urlsafe(32)
    request = pairing.request(owner["owner_id"], code, "PC")
    pairing.decide("local", request["request_id"], request["verification"], approve=True)
    issued = pairing.claim(request["request_id"], code)
    pairing.ack(request["request_id"], code, issued["credential"])
    pairing.activate_migration("local", confirm=True)

    rotated = tmp_path / "rotated.key"
    store.computers.issue(issued["computer_id"], rotated)
    assert store.computers.authenticate(issued["computer_id"], rotated.read_text().strip())
    assert not store.computers.authenticate(issued["computer_id"], issued["credential"])

    with pytest.raises(ValueError, match="pairing"):
        store.computers.issue("pc_new_legacy", tmp_path / "new.key", display_name="New")
    assert not (tmp_path / "new.key").exists()
    store.close()
