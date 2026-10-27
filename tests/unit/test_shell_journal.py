"""Shell intents are durable before spawn and cannot be replayed after recovery/expiry."""

import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from acp_gateway.connectors.policy import LocalPolicyError
from acp_gateway.connectors.shell_journal import ShellJournal
from acp_gateway.log import register_secret

ACTOR = "telegram:123"


def reserve(journal, operation=None, **overrides):
    return journal.reserve(
        operation or uuid4(),
        **{
            "actor": ACTOR,
            "alias": "codex",
            "session_id": "thread-1",
            "cwd": "/work",
            "command": "git status",
            **overrides,
        },
    )


def test_reserved_operation_is_durable_and_recovery_never_returns_it_for_execution(tmp_path):
    path = tmp_path / "shell.db"
    journal = ShellJournal(path)
    operation = uuid4()
    created, row = reserve(journal, operation)
    assert created and row["state"] == "reserved"
    journal.close()
    journal = ShellJournal(path)
    try:
        assert journal.recover() == 1
        assert journal.recover() == 0
        created, row = reserve(journal, operation, timeout=120.0)
        assert not created and row["state"] == "unknown" and row["reason"] == "interrupted"
        assert journal.activity() == {"running_sessions": 0, "uncertain_sessions": 1}
    finally:
        journal.close()


def test_atomic_duplicate_reservation_across_connections_launches_only_once(tmp_path):
    path = tmp_path / "shell.db"
    ShellJournal(path).close()
    operation = uuid4()
    barrier = threading.Barrier(4)

    def contender():
        journal = ShellJournal(path)
        try:
            barrier.wait(timeout=5)
            return reserve(journal, operation)[0]
        finally:
            journal.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: contender(), range(4)))
    assert outcomes.count(True) == 1 and outcomes.count(False) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"actor": "telegram:456"},
        {"alias": "claude"},
        {"session_id": "other"},
        {"cwd": "/other"},
        {"command": "git diff"},
        {"timeout": 1},
    ],
)
def test_operation_cannot_be_rebound_to_different_authority_or_payload(tmp_path, changes):
    journal = ShellJournal(tmp_path / "shell.db")
    operation = uuid4()
    try:
        reserve(journal, operation)
        with pytest.raises(LocalPolicyError, match="identity changed"):
            reserve(journal, operation, **changes)
        with pytest.raises(LocalPolicyError, match="unavailable"):
            journal.get(operation, actor="telegram:456")
    finally:
        journal.close()


def test_masking_settlement_retention_and_replay_tombstone(tmp_path):
    journal = ShellJournal(tmp_path / "shell.db")
    operation = uuid4()
    token = secrets.token_hex(32)
    register_secret(token)
    command = "echo " + token
    try:
        _, row = reserve(journal, operation, command=command)
        assert token not in row["command"]
        journal.running(operation, actor=ACTOR, interpreter="sh")
        with pytest.raises(LocalPolicyError, match="unverified"):
            journal.finish(operation, actor=ACTOR, state="completed", exit_code=0)
        result = journal.finish(
            operation,
            actor=ACTOR,
            state="completed",
            exit_code=0,
            cleanup_verified=True,
            output="Result " + token,
        )
        assert token not in result["output"]
        assert journal.activity() == {"running_sessions": 0, "uncertain_sessions": 0}
        with pytest.raises(LocalPolicyError, match="already settled"):
            journal.finish(operation, actor=ACTOR, state="unknown")
        assert journal.expire(before=time.time() + 1) == 1
        created, expired = reserve(journal, operation, command=command)
        assert not created and expired["state"] == "expired"
        assert expired["command"] == expired["output"] == expired["cwd"] == ""
    finally:
        journal.close()


def test_capacity_and_active_limits_do_not_delete_uuid_fences(tmp_path):
    journal = ShellJournal(tmp_path / "shell.db", max_records=2, max_running=1)
    first, second = uuid4(), uuid4()
    try:
        reserve(journal, first)
        with pytest.raises(LocalPolicyError, match="concurrency"):
            reserve(journal, second)
        journal.finish(first, actor=ACTOR, state="unknown")
        reserve(journal, second)
        journal.finish(
            second, actor=ACTOR, state="failed", reason="spawn_failed", cleanup_verified=True
        )
        journal.expire(before=time.time() + 1)
        with pytest.raises(LocalPolicyError, match="capacity"):
            reserve(journal)
        assert not reserve(journal, first)[0]
        assert journal.get(first, actor=ACTOR)["state"] == "unknown"
        assert not reserve(journal, second)[0]
    finally:
        journal.close()


def test_redaction_expansion_cannot_exceed_the_published_output_bound(tmp_path):
    journal = ShellJournal(tmp_path / "shell.db")
    try:
        operation = uuid4()
        reserve(journal, operation)
        result = journal.finish(
            operation,
            actor=ACTOR,
            state="completed",
            exit_code=0,
            cleanup_verified=True,
            output="key=x\n" * 10000,
        )
        assert result["output"] == "" and result["output_omitted"]
        assert result["reason"] == "output_limit" and result["state"] == "completed"
    finally:
        journal.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": float("nan")},
        {"timeout": True},
        {"timeout": 0},
        {"timeout": 601},
        {"command": ""},
        {"command": "x\x00y"},
        {"command": "x" * 4097},
        {"cwd": "../work"},
        {"cwd": "/work/../other"},
        {"actor": "telegram:abc"},
        {"alias": "bad/alias"},
        {"session_id": "bad\nidentity"},
    ],
)
def test_invalid_requests_do_not_reserve_an_operation(tmp_path, kwargs):
    journal = ShellJournal(tmp_path / "shell.db")
    try:
        with pytest.raises(LocalPolicyError):
            reserve(journal, **kwargs)
        assert journal.conn.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    finally:
        journal.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX private file modes")
def test_private_file_and_public_directory_refusal(tmp_path):
    path = tmp_path / "shell.db"
    journal = ShellJournal(path)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        journal.close()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        ShellJournal(public / "shell.db")
