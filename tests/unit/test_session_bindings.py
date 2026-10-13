import secrets

import pytest

from acp_gateway.agents.bindings import session_binding
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, Store

BINDING = {
    "backend": "hermes",
    "mode": "dedicated",
    "profile": "default",
    "runtime_id": "live-1",
    "stored_id": "stored-1",
    "restore": "disabled",
}


def test_binding_persists_without_changing_schema_or_conversation_ownership(tmp_path):
    path = tmp_path / "gateway.db"
    conversation = Conversation("telegram", "home-chat", "home/hermes")
    store = Store.open(path)
    record = store.add_session(conversation, "handle-1", "/work", binding=BINDING)
    version = store.schema_version
    store.close()
    store = Store.open(path)
    try:
        restored = store.active_session(conversation)
        assert restored == record
        assert restored.binding == BINDING and restored.availability == "unknown"
        assert store.schema_version == version
        assert store.sessions(Conversation("telegram", "other-chat", "home/hermes")) == []
        assert store.sessions(Conversation("telegram", "home-chat", "work/hermes")) == []
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["extra", "credential", "long", "restore", "wrong_type"])
def test_invalid_binding_never_creates_session_or_stores_credentials(mutation):
    value = dict(BINDING)
    credential = secrets.token_urlsafe(32)
    register_secret(credential)
    if mutation == "extra":
        value["token"] = credential
    elif mutation == "credential":
        value["stored_id"] = credential
    elif mutation == "long":
        value["profile"] = "x" * 257
    elif mutation == "restore":
        value["restore"] = "automatic"
    else:
        value["stored_id"] = []
    store = Store.open(":memory:")
    conversation = Conversation("cli", "default", "hermes")
    try:
        with pytest.raises(ValueError) as exc:
            store.add_session(conversation, "handle", "/work", binding=value)
        assert credential not in str(exc.value)
        assert store.sessions(conversation) == []
    finally:
        store.close()
    assert session_binding(None) is None
