"""Explicit work profiles preserve safe defaults and custom resource limits."""

import pytest
from pydantic import ValidationError

from acp_gateway.cli.wizard import _agent, _profiles
from acp_gateway.config import ClaudeCodeSettings, CodexSettings
from unit.test_codex_cli import Answers


@pytest.mark.parametrize("settings", [CodexSettings, ClaudeCodeSettings])
def test_profile_defaults_and_explicit_work_limits(settings):
    assert settings().mode == "safe"
    assert settings().turn_timeout == 600
    work = settings(mode="work")
    assert work.turn_timeout == 3600
    assert settings(mode="work", turn_timeout=123).turn_timeout == 123
    assert settings.model_validate(work.model_dump()) == work
    if settings is ClaudeCodeSettings:
        assert work.max_turns == 100
        assert settings(mode="work", max_turns=23).max_turns == 23
    else:
        assert work.sandbox == "workspace-write"
    with pytest.raises(ValidationError):
        settings(mode="bypass")


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
def test_setup_work_profile_has_work_defaults(kind, tmp_path):
    result = _agent(
        Answers(
            **{
                "Agent type": kind,
                "Agent permission profile": "work",
                "Working directory on the agent computer": str(tmp_path),
            }
        ),
        {},
    )
    assert result[kind]["mode"] == "work"
    assert result[kind]["turn_timeout"] == 3600
    if kind == "claude_code":
        assert result[kind]["max_turns"] == 100


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
def test_setup_offers_two_separate_profiles(kind, tmp_path):
    raw = {}
    _profiles(
        Answers(
            **{
                "Agent type": kind,
                "Create both safe and work profiles?": True,
                "Working directory on the agent computer": str(tmp_path),
            }
        ),
        raw,
        {},
        "connector",
    )
    safe, work = raw["agents"]
    assert safe[kind]["mode"] == "safe"
    assert work[kind]["mode"] == "work"
    assert safe["alias"] != work["alias"]
    assert work[kind]["turn_timeout"] == 3600


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
def test_switching_mode_preserves_explicit_limits_equal_to_safe_defaults(kind, tmp_path):
    old = {
        "alias": "agent",
        "kind": kind,
        "backend": kind,
        "default_cwd": str(tmp_path),
        kind: {"turn_timeout": 600},
    }
    if kind == "claude_code":
        old[kind]["max_turns"] = 16
    edited = _agent(Answers(**{"Agent permission profile": "work"}), {}, old=old)
    assert edited[kind]["turn_timeout"] == 600
    if kind == "claude_code":
        assert edited[kind]["max_turns"] == 16
