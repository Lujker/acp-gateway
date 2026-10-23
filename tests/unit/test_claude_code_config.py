"""Managed Claude Code configuration is local, bounded and never a bypass."""

import pytest
from pydantic import ValidationError

from acp_gateway.config import AgentProfile


def local(**values):
    return AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd="/work", **values
    )


def test_managed_claude_profile_has_safe_bounded_defaults():
    profile = local()
    assert profile.claude_code.executable == "claude"
    assert profile.claude_code.model is None
    assert profile.claude_code.request_timeout == 30
    assert profile.claude_code.turn_timeout == 600
    assert profile.claude_code.max_turns == 16
    assert profile.claude_code.max_sessions == 8
    assert not profile.url and profile.secret_env is None


@pytest.mark.parametrize(
    "settings",
    [
        {"executable": "--skip"},
        {"executable": "claude --dangerously-skip-permissions"},
        {"executable": "https://host/claude"},
        {"executable": "./claude"},
        {"model": "--bad"},
        {"request_timeout": 0},
        {"turn_timeout": float("inf")},
        {"max_turns": 0},
        {"max_sessions": 33},
        {"permission_mode": "bypassPermissions"},
        {"extra_args": ["--dangerously-skip-permissions"]},
        {"sandbox": "danger-full-access"},
    ],
)
def test_unsafe_claude_settings_are_rejected(settings):
    with pytest.raises(ValidationError):
        local(claude_code=settings)


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "ws://localhost:1234"},
        {"secret_env": "AGENT_SECRET"},
        {"computer_id": "home"},
        {"hermes": {}},
        {"codex": {}},
        {"session_mode": "auto"},
        {"allow_insecure_transport": True},
    ],
)
def test_local_claude_profile_rejects_foreign_transport_settings(fields):
    with pytest.raises(ValidationError):
        local(**fields)


def test_routed_claude_profile_contains_no_local_cli_settings():
    profile = AgentProfile(
        alias="claude",
        kind="claude_code",
        backend="connector",
        computer_id="home",
        default_cwd="C:\\work",
    )
    assert profile.claude_code is None
    assert profile.address == "home/claude"
    with pytest.raises(ValidationError):
        AgentProfile.model_validate({**profile.model_dump(), "claude_code": {}})


@pytest.mark.parametrize("kind,backend", [("generic", "claude_code"), ("claude_code", "remote")])
def test_claude_kind_backend_pair_must_match(kind, backend):
    with pytest.raises(ValidationError):
        AgentProfile(alias="claude", kind=kind, backend=backend, default_cwd="/work")


def test_claude_profile_accepts_explicit_native_executable_and_model():
    profile = local(
        claude_code={
            "executable": "C:/Program Files/Claude/claude.exe",
            "model": "claude-sonnet-4-6",
            "request_timeout": 15,
            "max_turns": 4,
        }
    )
    assert profile.claude_code.executable == "C:/Program Files/Claude/claude.exe"
    assert profile.claude_code.max_turns == 4
