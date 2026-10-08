"""Relay routing must never require the computer's agent credentials on the VPS."""

import pytest
from pydantic import ValidationError

from acp_gateway.config import AgentProfile, Settings


def route(computer="work", **fields):
    return dict(
        alias="goose", backend="connector", computer_id=computer, default_cwd="/work", **fields
    )


def test_namespaced_routes_allow_identical_local_aliases():
    settings = Settings(connector={"enabled": True}, agents=[route(), route("home")])
    assert [p.address for p in settings.agents] == ["work/goose", "home/goose"]
    assert settings.agent("home/goose").computer_id == "home"
    assert settings.agent("work/goose").url == ""


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "ws://127.0.0.1/acp"},
        {"secret_env": "LOCAL_SECRET"},
        {"tls_fingerprint": "AB" * 32},
        {"allow_insecure_transport": True},
    ],
)
def test_computer_credentials_and_endpoints_are_refused_on_vps(fields):
    with pytest.raises(ValidationError, match="belong on the computer"):
        AgentProfile(**route(**fields))


def test_relay_requires_explicit_ingress_and_computer():
    with pytest.raises(ValidationError, match=r"connector\.enabled"):
        Settings(agents=[route()])
    with pytest.raises(ValidationError, match="requires computer_id"):
        AgentProfile(alias="goose", backend="connector", default_cwd="/work")
    with pytest.raises(ValidationError, match="requires backend"):
        AgentProfile(
            alias="goose", computer_id="work", url="ws://127.0.0.1/acp", default_cwd="/work"
        )


def test_duplicate_routes_and_mcp_name_collisions_are_refused():
    with pytest.raises(ValidationError, match="duplicate"):
        Settings(connector={"enabled": True}, agents=[route(), route()])
    with pytest.raises(ValidationError, match="MCP tool names"):
        Settings(
            connector={"enabled": True},
            agents=[
                route(),
                dict(alias="work__goose", url="ws://127.0.0.1/acp", default_cwd="/work"),
            ],
        )


@pytest.mark.parametrize(
    "listener",
    [
        {"connect_path": "/../connect"},
        {"tls_cert": "/cert.pem"},
        {"port": 0},
    ],
)
def test_invalid_ingress_configuration_is_refused(listener):
    with pytest.raises(ValidationError):
        Settings(connector=listener)
