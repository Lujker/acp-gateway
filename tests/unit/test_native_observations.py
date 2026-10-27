"""Native evidence projection is bounded, privacy-safe and never a setter ack."""

import secrets

import pytest

from acp_gateway.agents.observations import claude_observation, codex_observation, observation
from acp_gateway.log import register_secret
from acp_gateway.storage import Conversation, JobStatus, Store


@pytest.mark.parametrize("value", [None, [], {}, {"source": []}, {"source": "model.options"}])
def test_unknown_metadata_is_ignored_without_breaking_session(value):
    assert observation(value) is None


def test_projection_keeps_only_native_identifiers_and_does_not_infer_defaults():
    value = codex_observation(
        {
            "model": "test-model",
            "modelProvider": "test-provider",
            "reasoningEffort": None,
            "config": {"reasoningEffort": "medium"},
            "credentials": "not projected",
        },
        loading=False,
    )
    assert value == {
        "model": "test-model",
        "provider": "test-provider",
        "effort": None,
        "source": "codex.thread.start",
    }
    assert codex_observation({}, loading=True) == {
        "model": None,
        "provider": None,
        "effort": None,
        "source": "codex.thread.resume",
    }


@pytest.mark.parametrize("bad", [[], {}, "a" * 257, "<bad>", "bad\nline"])
def test_invalid_fields_are_unknown_while_valid_fields_remain(bad):
    assert codex_observation(
        {"model": bad, "modelProvider": "custom", "reasoningEffort": bad}, loading=False
    ) == {"model": None, "provider": "custom", "effort": None, "source": "codex.thread.start"}


def test_registered_secret_cannot_be_stored_as_model_identifier():
    credential = secrets.token_hex(32)
    register_secret(credential)
    assert codex_observation({"model": credential}, loading=False)["model"] is None
    assert claude_observation({"model": credential})["model"] is None


def test_claude_init_cannot_claim_provider_or_effort():
    assert observation(
        {
            "model": "native-model",
            "provider": "unverified",
            "effort": "high",
            "source": "claude.system.init",
        }
    ) == {"model": "native-model", "provider": None, "effort": None, "source": "claude.system.init"}


def test_job_snapshot_is_pinned_once_and_cannot_modify_finished_answer(tmp_path):
    store = Store.open(tmp_path / "gateway.db")
    try:
        session = store.add_session(Conversation("telegram", "123", "code"), "sid", "/work")
        store.add_job("test", session.id, context={"kind": "codex", "cwd": "/work"})
        value = codex_observation({"model": "chosen", "reasoningEffort": "high"}, loading=True)
        store.observe_job_model("test", value)
        assert store.job_context("test")["model"] == "chosen"
        assert store.job_context("test")["effort"] == "high"
        with pytest.raises(ValueError, match="already pinned"):
            store.observe_job_model("test", None)
        store.finish_job("test", JobStatus.COMPLETED, answer="done")
        with pytest.raises(ValueError, match="unfinished"):
            store.observe_job_model("test", None)
        assert store.job_context("test")["model"] == "chosen"
        assert store.models.state(session.id) is None
    finally:
        store.close()


def test_acp_config_options_project_recorded_goose_model_provider_and_effort():
    from acp.schema import NewSessionResponse

    from acp_gateway.agents.observations import config_observation
    from fakes.recordings import result_of

    raw = result_of("modes", "session/new")
    response = NewSessionResponse.model_validate(raw)
    expected = {
        "provider": "chatgpt_codex",
        "model": "gpt-5.6-sol",
        "effort": "medium",
        "source": "acp.config_options",
    }
    assert config_observation(response.config_options) == expected
    assert config_observation(raw["configOptions"]) == expected


@pytest.mark.parametrize(
    "options",
    [
        None,
        [],
        [{"id": "provider", "type": "select", "currentValue": "p", "options": []}],
        [{"id": "m", "category": "model", "type": "boolean", "currentValue": True}],
        [{"id": "m", "category": "model", "type": "select", "currentValue": "bad value"}],
    ],
)
def test_acp_config_options_without_valid_model_are_unknown(options):
    from acp_gateway.agents.observations import config_observation

    assert config_observation(options) is None


def test_acp_config_effort_outside_known_levels_and_custom_category_provider_are_dropped():
    from acp_gateway.agents.observations import config_observation

    value = config_observation(
        [
            {"id": "model", "category": "model", "type": "select", "currentValue": "m-1"},
            {"id": "effort", "category": "thought_level", "type": "select", "currentValue": "x"},
            {"id": "provider", "category": "mode", "type": "select", "currentValue": "p"},
        ]
    )
    assert value == {
        "provider": None,
        "model": "m-1",
        "effort": None,
        "source": "acp.config_options",
    }
