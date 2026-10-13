"""Validate the connector result boundary, including secret-bearing extra fields."""

import pytest

from acp_gateway.agents.model_control import MODEL_OPTIONS, MODEL_SET, model_result


def catalogue():
    return {
        "provider": "provider-a",
        "model": "model-a",
        "pending": None,
        "providers": [{"provider": "provider-a", "models": ["model-a"], "api_key": "private"}],
        "observation_source": "model.options",
        "base_url": "private-endpoint",
    }


def test_remote_catalogue_does_not_forward_keys_endpoints_or_unknown_fields():
    result = model_result(MODEL_OPTIONS, catalogue())
    assert "base_url" not in result
    assert result["providers"] == [{"provider": "provider-a", "models": ["model-a"]}]
    assert model_result(MODEL_SET, {"status": "accepted", "api_key": "private"}) == {
        "status": "accepted"
    }


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "x --global"},
        {"model": None},
        {"providers": {}},
        {"providers": [{"provider": "p", "models": "not-a-list"}]},
        {"pending": True},
        {"pending": {"provider": "p", "model": "--global"}},
        {"observation_source": "session.info"},
    ],
)
def test_remote_catalogue_invalid_observation_is_rejected(change):
    with pytest.raises(ValueError):
        model_result(MODEL_OPTIONS, catalogue() | change)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "applied"},
        {"status": "confirmation_required"},
        {"status": "confirmation_required", "confirmation_token": "a" * 32, "message": "bad\x00"},
        {"status": "confirmation_required", "confirmation_token": True, "message": "warning"},
    ],
)
def test_remote_set_response_cannot_forge_application_or_confirmation(payload):
    with pytest.raises(ValueError):
        model_result(MODEL_SET, payload)
