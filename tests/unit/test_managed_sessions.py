"""Durable native bindings fence scope changes, simultaneous owners and lost turns."""

import pytest

from acp_gateway.adapters.managed_sessions import ManagedSessions
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.policy import LocalPolicyError


def profile(tmp_path, **settings):
    return AgentProfile(
        alias="code", kind="codex", backend="codex", default_cwd=str(tmp_path), codex=settings
    )


def test_idle_binding_survives_restart_with_exact_scope(tmp_path):
    p = profile(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    first.create("native-id", str(tmp_path))
    first.begin("native-id")
    first.finish("native-id")
    first.close()
    resumed = ManagedSessions(tmp_path / "bindings", p)
    resumed.load("native-id", str(tmp_path))
    resumed.close()
    for changed in (profile(tmp_path, mode="work"), p.model_copy(update={"alias": "other"})):
        with pytest.raises(LocalPolicyError):
            ManagedSessions(tmp_path / "bindings", changed).load("native-id", str(tmp_path))


def test_binding_rejects_second_owner_unknown_id_and_wrong_cwd(tmp_path):
    p = profile(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    second = ManagedSessions(tmp_path / "bindings", p)
    try:
        first.create("native-id", str(tmp_path))
        for sid, cwd in (
            ("native-id", str(tmp_path)),
            ("unknown", str(tmp_path)),
            ("native-id", str(tmp_path / "elsewhere")),
        ):
            with pytest.raises(LocalPolicyError):
                second.load(sid, cwd)
    finally:
        first.close()
        second.close()


def test_lost_turn_remains_blocked_across_process_lifetimes(tmp_path):
    p = profile(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    first.create("native-id", str(tmp_path))
    first.begin("native-id")
    first.close()
    after = ManagedSessions(tmp_path / "bindings", p)
    with pytest.raises(LocalPolicyError, match="unknown"):
        after.load("native-id", str(tmp_path))
    after.close()


def test_unconfirmed_cleanup_never_releases_idle_binding_for_resume(tmp_path):
    p = profile(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    first.create("native-id", str(tmp_path))
    first.close(confirmed=False)
    after = ManagedSessions(tmp_path / "bindings", p)
    with pytest.raises(LocalPolicyError, match="unknown"):
        after.load("native-id", str(tmp_path))
    after.close()


def test_wsl_distribution_user_and_profile_home_are_part_of_binding(tmp_path):
    def wsl_profile(**wsl):
        return AgentProfile(
            alias="code",
            kind="codex",
            backend="codex",
            default_cwd="/project",
            codex={
                "executable": "/bin/codex",
                "wsl": {"distribution": "Ubuntu", "user": "alice", **wsl},
            },
        )

    first = ManagedSessions(tmp_path / "bindings", wsl_profile())
    first.create("native-id", "/project")
    first.close()
    for changes in ({"distribution": "Debian"}, {"user": "bob"}, {"agent_home": "/other"}):
        other = ManagedSessions(tmp_path / "bindings", wsl_profile(**changes))
        with pytest.raises(LocalPolicyError):
            other.load("native-id", "/project")
        other.close()
