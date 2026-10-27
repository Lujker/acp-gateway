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


def claude_profile(tmp_path):
    return AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd=str(tmp_path)
    )


def test_claude_empty_binding_remains_empty_until_first_turn(tmp_path):
    p = claude_profile(tmp_path)
    directory = tmp_path / "bindings"
    first = ManagedSessions(directory, p)
    first.create("native-id", str(tmp_path), empty=True)
    first.close()
    for _ in range(2):
        fresh = ManagedSessions(directory, p)
        assert fresh.load("native-id", str(tmp_path)) is False
        fresh.close()
    used = ManagedSessions(directory, p)
    assert used.load("native-id", str(tmp_path)) is False
    used.begin("native-id")
    used.finish("native-id")
    used.close()
    after = ManagedSessions(directory, p)
    assert after.load("native-id", str(tmp_path)) is True
    after.close()


@pytest.mark.parametrize("begin,confirmed", [(True, True), (False, False)])
def test_claude_empty_binding_never_bypasses_uncertain_fence(tmp_path, begin, confirmed):
    p = claude_profile(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    first.create("native-id", str(tmp_path), empty=True)
    if begin:
        first.begin("native-id")
    first.close(confirmed=confirmed)
    after = ManagedSessions(tmp_path / "bindings", p)
    with pytest.raises(LocalPolicyError, match="unknown"):
        after.load("native-id", str(tmp_path))
    after.close()


def test_legacy_claude_idle_binding_requires_native_resume(tmp_path):
    p = claude_profile(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    first.create("native-id", str(tmp_path))
    first.close()
    after = ManagedSessions(tmp_path / "bindings", p)
    assert after.load("native-id", str(tmp_path)) is True
    after.close()


def test_empty_claude_binding_still_requires_exact_scope_cwd_and_exclusive_owner(tmp_path):
    p = claude_profile(tmp_path)
    directory = tmp_path / "bindings"
    first = ManagedSessions(directory, p)
    first.create("native-id", str(tmp_path), empty=True)
    second = ManagedSessions(directory, p)
    with pytest.raises(LocalPolicyError):
        second.load("native-id", str(tmp_path))
    first.close()
    with pytest.raises(LocalPolicyError):
        second.load("native-id", str(tmp_path / "other"))
    changed = p.model_copy(update={"alias": "another"})
    with pytest.raises(LocalPolicyError):
        ManagedSessions(directory, changed).load("native-id", str(tmp_path))
    assert second.load("native-id", str(tmp_path)) is False
    second.close()


def test_interrupted_turn_needs_confirmed_stop_and_explicit_resume(tmp_path):
    from acp_gateway.adapters.managed_sessions import InterruptedTurn

    p = profile(tmp_path)
    cwd = str(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    first.create("native-id", cwd)
    first.begin("native-id")
    # Unproven process stop: the unknown turn stays fenced as before.
    first.close(confirmed=False, interrupted=True)
    second = ManagedSessions(tmp_path / "bindings", p)
    with pytest.raises(LocalPolicyError, match="unknown") as raised:
        second.load("native-id", cwd)
    assert not isinstance(raised.value, InterruptedTurn)
    with pytest.raises(LocalPolicyError, match="unknown"):
        second.load("native-id", cwd, resume_interrupted=True)
    second.close()

    third = ManagedSessions(tmp_path / "bindings", p)
    third._acquire("native-id")  # the owner that still holds the busy lease
    third.close(confirmed=True, interrupted=True)
    after = ManagedSessions(tmp_path / "bindings", p)
    with pytest.raises(InterruptedTurn):
        after.load("native-id", cwd)
    with pytest.raises(LocalPolicyError):
        after.load("native-id", str(tmp_path / "elsewhere"), resume_interrupted=True)
    assert after.load("native-id", cwd, resume_interrupted=True) is True
    after.close()
    # Resumed and closed cleanly: an ordinary idle dialogue again.
    again = ManagedSessions(tmp_path / "bindings", p)
    assert again.load("native-id", cwd) is True
    again.close()


def test_default_close_keeps_unknown_turn_fenced(tmp_path):
    # Codex keeps the strict fence: no interrupted state without opting in.
    p = profile(tmp_path)
    first = ManagedSessions(tmp_path / "bindings", p)
    first.create("native-id", str(tmp_path))
    first.begin("native-id")
    first.close()
    after = ManagedSessions(tmp_path / "bindings", p)
    with pytest.raises(LocalPolicyError, match="unknown"):
        after.load("native-id", str(tmp_path), resume_interrupted=True)
    after.close()
