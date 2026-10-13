"""Restart-safe model evidence; matching identifiers are not proof of application."""

import pytest

from acp_gateway.storage import Conversation, Store


def observation(provider="provider-b", model="model-b", pending=None):
    return {
        "provider": provider,
        "model": model,
        "pending": pending,
        "observation_source": "model.options",
    }


def test_intent_survives_reopen_and_becomes_unknown_without_replay(tmp_path):
    path = tmp_path / "models.db"
    store = Store.open(path)
    session = store.add_session(Conversation("cli", "default", "hermes"), "sid", "/workspace")
    store.models.observe(session.id, observation("provider-a", "model-a"))
    store.models.begin(session.id, "provider-b", "model-b", 7)
    store.close()
    store = Store.open(path)
    try:
        assert store.models.recover_intents() == 1
        state = store.models.state(session.id)
        assert state["state"] == "unknown"
        assert state["selection_revision"] == 7
        assert state["observed_model"] == "model-a"
        store.models.observe(session.id, observation())
        assert store.models.state(session.id)["state"] == "unknown"
        assert store.models.unresolved_count() == 1
        assert store.models.recover_intents() == 0
    finally:
        store.close()


@pytest.mark.parametrize("deferred", [False, True])
def test_pending_requires_matching_fresh_observation_and_own_turn_if_deferred(deferred):
    store = Store.open(":memory:")
    try:
        session = store.add_session(Conversation("cli", "default", "hermes"), "sid", "/workspace")
        store.models.begin(session.id, "provider-b", "model-b", 1)
        store.models.settle(session.id, "pending", deferred=deferred)
        store.models.observe(session.id, observation("provider-a", "model-a"))
        assert store.models.state(session.id)["state"] == "pending"
        store.models.observe(
            session.id, observation(pending={"provider": "provider-b", "model": "model-b"})
        )
        assert store.models.state(session.id)["state"] == "pending"
        store.models.observe(session.id, observation())
        assert store.models.state(session.id)["state"] == ("pending" if deferred else "observed")
        if deferred:
            store.models.turn_finished(session.id)
            assert store.models.state(session.id)["state"] == "pending"
            store.models.observe(session.id, observation())
            assert store.models.state(session.id)["state"] == "observed"
        assert store.models.unresolved_count() == 0
    finally:
        store.close()


def test_model_intent_and_operation_identity_are_atomic():
    import sqlite3

    store = Store.open(":memory:")
    try:
        session = store.add_session(Conversation("cli", "default", "hermes"), "sid", "/workspace")
        store.models.observe(session.id, observation("provider-a", "model-a"))
        store._conn.execute(
            "CREATE TRIGGER fail_intent_metadata BEFORE INSERT ON channel_state "
            "WHEN NEW.namespace = 'model_operation_id' BEGIN "
            "SELECT RAISE(ABORT, 'synthetic write failure'); END;"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.models.begin(session.id, "provider-b", "model-b", 1, operation_id="a" * 32)
        assert store.models.state(session.id)["state"] == "observed"
        assert store.models.state(session.id)["operation_id"] is None
    finally:
        store.close()
