"""Managed Codex is local, conservative, and cannot inherit remote credentials."""

import pytest
from pydantic import ValidationError

from acp_gateway.config import AgentProfile


def test_codex_profile_has_conservative_local_defaults():
    profile = AgentProfile(alias="code", kind="codex", backend="codex", default_cwd="/work")
    assert profile.codex.executable == "codex"
    assert profile.codex.sandbox == "read-only"
    assert profile.codex.model is None
    assert not profile.secret_env


@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "generic"},
        {"url": "ws://localhost/acp"},
        {"secret_env": "REMOTE_SECRET"},
        {"computer_id": "pc"},
        {"hermes": {}},
        {"session_mode": "auto"},
        {"tls_fingerprint": "AA" * 32},
        {"allow_insecure_transport": True},
        {"codex": {"executable": "codex --yolo"}},
        {"codex": {"executable": "./codex"}},
        {"codex": {"executable": "https://example.org/codex"}},
        {"codex": {"sandbox": "danger-full-access"}},
        {"codex": {"args": ["--yolo"]}},
        {"default_cwd": "relative"},
    ],
)
def test_codex_rejects_unsafe_local_combinations(extra):
    values = dict(alias="code", kind="codex", backend="codex", default_cwd="/work")
    values.update(extra)
    with pytest.raises(ValidationError):
        AgentProfile(**values)


def test_codex_settings_belong_only_on_local_managed_backend():
    with pytest.raises(ValidationError):
        AgentProfile(
            alias="code", kind="codex", backend="remote", url="ws://localhost", default_cwd="/work"
        )
    with pytest.raises(ValidationError):
        AgentProfile(
            alias="code",
            kind="codex",
            backend="connector",
            computer_id="pc",
            default_cwd="/work",
            codex={},
        )
    relay = AgentProfile(
        alias="code", kind="codex", backend="connector", computer_id="pc", default_cwd="/work"
    )
    assert relay.codex is None


@pytest.mark.parametrize(
    "executable", [r"\\server\share\codex.exe", "//server/share/codex", "/usr/bin/codex --yolo"]
)
def test_managed_executable_must_be_local_and_not_a_command_line(executable):
    with pytest.raises(ValidationError):
        AgentProfile(
            alias="code",
            kind="codex",
            backend="codex",
            default_cwd="/work",
            codex={"executable": executable},
        )
