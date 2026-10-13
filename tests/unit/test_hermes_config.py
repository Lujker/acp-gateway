import pytest
from pydantic import ValidationError

from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
from acp_gateway.connectors.launch import local_policies
from acp_gateway.connectors.policy import LocalAgentPolicy, LocalPolicyError


def local(**overrides):
    return AgentProfile(
        **(
            {"alias": "hermes", "kind": "hermes", "backend": "hermes", "default_cwd": r"C:\work"}
            | overrides
        )
    )


def test_local_hermes_and_vps_route_have_no_agent_token_or_fixed_port():
    profile = local()
    assert profile.hermes.profile == "default" and profile.url == ""
    settings = Settings(
        connector={"enabled": True},
        agents=[
            {
                "alias": "hermes",
                "kind": "hermes",
                "backend": "connector",
                "computer_id": "home",
                "default_cwd": r"C:\work",
            }
        ],
    )
    assert settings.agents[0].address == "home/hermes"
    config = AppConfig(
        Settings(agents=[profile], policy={"max_prompt_length": 42}), SecretStore({}), None, None
    )
    assert local_policies(config)["hermes"].max_prompt_length == 42


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "http://127.0.0.1:4549"},
        {"secret_env": "HERMES_SECRET"},
        {"session_mode": "auto"},
        {"computer_id": "home"},
        {"kind": "generic"},
        {"hermes": {"lock_dir": "relative"}},
    ],
)
def test_hermes_attach_rejects_unsafe_or_ambiguous_configuration(fields):
    with pytest.raises(ValidationError):
        local(**fields)


def test_hermes_rpc_cannot_be_misconfigured_as_remote_acp():
    with pytest.raises(ValidationError, match="JSON-RPC"):
        local(backend="remote", url="ws://127.0.0.1:4549")
    with pytest.raises(ValidationError, match="hermes settings"):
        local(backend="connector", computer_id="home", hermes={})


@pytest.mark.parametrize("cwd", [r"C:\work", "c:/WORK", "C:/work/"])
def test_native_windows_allowlist_canonicalizes_case_and_separator(cwd):
    policy = LocalAgentPolicy((r"C:\work",))
    message = policy.prepare(
        {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": cwd}}
    )
    assert message["params"]["cwd"] == r"C:\work"


@pytest.mark.parametrize(
    "cwd",
    [
        r"C:\work\..\work",
        r"C:\work\child",
        r"C:work",
        r"D:\work",
        r"\\host\share\work",
        r"\\?\C:\work",
        r"C:\work.",
        r"C:\work ",
        r"C:\work:stream",
        "/work",
        "C:/work-evil",
    ],
)
def test_native_windows_cwd_rejects_traversal_devices_streams_and_other_roots(cwd):
    policy = LocalAgentPolicy((r"C:\work",))
    with pytest.raises(LocalPolicyError):
        policy.prepare({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": cwd}})
