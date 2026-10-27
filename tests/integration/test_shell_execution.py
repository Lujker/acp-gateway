"""Real model-free work commands behind the Linux owned-group supervisor."""

import asyncio
import secrets
import shlex
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from acp_gateway.adapters.managed_sessions import ManagedSessions
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.policy import LocalPolicyError
from acp_gateway.connectors.shell_runner import ShellRunner
from acp_gateway.connectors.shell_service import LocalShell
from acp_gateway.log import register_secret

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real Linux shell supervisor")
ACTOR = "telegram:123"


def profile(tmp_path, mode="work"):
    return AgentProfile(
        alias="code", kind="codex", backend="codex", default_cwd=str(tmp_path), codex={"mode": mode}
    )


@pytest.fixture
async def local(tmp_path):
    configured = [profile(tmp_path)]
    bindings = ManagedSessions(tmp_path / "bindings", configured[0])
    bindings.create("owned", str(tmp_path))
    service = LocalShell(
        tmp_path / "shell.db",
        profiles=lambda: configured,
        authorize=lambda alias, sid, cwd: bindings.contains(sid, cwd),
        admission=lambda: None,
    )
    try:
        yield service, configured, bindings
    finally:
        await service.close()
        bindings.close()


def start(local, command, operation=None, **kwargs):
    service, configured, _ = local
    return service.start(
        operation or uuid4(),
        actor=ACTOR,
        alias="code",
        session_id="owned",
        cwd=configured[0].default_cwd,
        command=command,
        **kwargs,
    )


async def settled(service, operation):
    async with asyncio.timeout(10):
        while (row := service.status(operation, actor=ACTOR))["state"] in {"reserved", "running"}:
            await asyncio.sleep(0.01)
        return row


async def test_shell_runs_in_exact_owned_cwd_and_reports_unicode_stderr_and_exit(local, tmp_path):
    row = start(local, "printf '雪☃\n'; pwd; printf error >&2; exit 7")
    result = await settled(local[0], row["id"])
    assert result["state"] == "failed" and result["exit_code"] == 7
    assert "雪☃\n" in result["output"] and str(tmp_path) in result["output"]
    assert "[stderr]\nerror" in result["output"]
    assert result["cleanup_verified"] and result["shell"] == "sh"
    assert "digest" not in result
    assert local[0].maintenance_status() == {"running_sessions": 0, "uncertain_sessions": 0}


async def test_stdin_is_eof_and_duplicate_start_never_executes_again(local, tmp_path):
    operation = uuid4()
    command = "if read value; then exit 9; fi; printf once >> count.txt; printf done"
    first = start(local, command, operation)
    duplicate = start(local, command, operation)
    assert duplicate["id"] == first["id"]
    assert (await settled(local[0], operation))["state"] == "completed"
    assert start(local, command, operation)["state"] == "completed"
    assert (tmp_path / "count.txt").read_text() == "once"


async def test_gateway_and_telegram_credentials_are_not_inherited(local, monkeypatch):
    monkeypatch.setenv("ACPGW_GATEWAY__API_TOKEN", secrets.token_hex(32))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", secrets.token_hex(32))
    row = start(
        local,
        'test -z "$ACPGW_GATEWAY__API_TOKEN" && test -z "$TELEGRAM_BOT_TOKEN" && printf clean',
    )
    result = await settled(local[0], row["id"])
    assert result["state"] == "completed" and result["output"] == "clean"


async def test_large_output_is_omitted_without_a_partial_secret_prefix(local):
    credential = secrets.token_hex(1024)
    register_secret(credential)
    source = f"import sys;sys.stdout.write('x'*59000+{credential!r})"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(source)
    row = start(local, command)
    result = await settled(local[0], row["id"])
    assert result["state"] == "completed" and result["exit_code"] == 0
    assert result["output_omitted"] and result["output"] == ""
    assert result["reason"] == "output_limit" and result["cleanup_verified"]


async def test_command_and_final_output_are_redacted_before_status(local):
    credential = secrets.token_hex(32)
    register_secret(credential)
    row = start(local, "printf " + shlex.quote(credential))
    assert credential not in row["command"]
    result = await settled(local[0], row["id"])
    assert result["state"] == "completed" and result["output"]
    assert credential not in result["output"] and credential not in result["command"]


async def test_timeout_stops_owned_group(local):
    row = start(local, "sleep 30", timeout=0.1)
    result = await settled(local[0], row["id"])
    assert result["state"] == "timed_out" and result["cleanup_verified"]


async def test_cancel_is_actor_owned_and_closes_active_group(local, tmp_path):
    ready = tmp_path / "ready"
    row = start(local, f"printf ready > {shlex.quote(str(ready))}; sleep 30")
    async with asyncio.timeout(5):
        while not ready.exists():
            await asyncio.sleep(0.01)
    with pytest.raises(LocalPolicyError, match="unavailable"):
        local[0].cancel(row["id"], actor="telegram:456")
    local[0].cancel(row["id"], actor=ACTOR)
    result = await settled(local[0], row["id"])
    assert result["state"] == "cancelled" and result["cleanup_verified"]


async def test_background_descendant_is_cleaned_without_stopping_a_sibling(local, tmp_path):
    sibling = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time;time.sleep(30)"
    )
    try:
        pid_file = tmp_path / "child.pid"
        row = start(local, f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}; exit 0")
        result = await settled(local[0], row["id"])
        assert result["state"] == "completed" and result["cleanup_verified"]
        child = int(pid_file.read_text())
        stat = Path(f"/proc/{child}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"
        assert sibling.returncode is None
    finally:
        if sibling.returncode is None:
            sibling.kill()
        await sibling.wait()


async def test_safe_unknown_session_and_other_cwd_cannot_reach_spawn(local, tmp_path):
    service, configured, bindings = local
    with pytest.raises(LocalPolicyError, match="cwd"):
        service.start(
            uuid4(),
            actor=ACTOR,
            alias="code",
            session_id="owned",
            cwd="/elsewhere",
            command="echo forbidden",
        )
    with pytest.raises(LocalPolicyError, match="not owned"):
        service.start(
            uuid4(),
            actor=ACTOR,
            alias="code",
            session_id="foreign",
            cwd=str(tmp_path),
            command="echo forbidden",
        )
    configured[0] = profile(tmp_path, "safe")
    with pytest.raises(LocalPolicyError, match="work profile"):
        start(local, "echo forbidden")
    assert service.journal.conn.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    assert bindings.contains("owned", str(tmp_path))


async def test_runner_exception_is_unknown_not_claimed_as_verified_cleanup(local, monkeypatch):
    async def failed(*args, **kwargs):
        raise OSError("synthetic runner failure")

    monkeypatch.setattr(ShellRunner, "run", failed)
    row = start(local, "echo test")
    result = await settled(local[0], row["id"])
    assert result["state"] == "unknown" and not result["cleanup_verified"]
    assert local[0].maintenance_status()["uncertain_sessions"] == 1


async def test_shutdown_cancels_work_and_closes_exclusive_journal_owner(local, tmp_path):
    service, configured, bindings = local
    row = start(local, "sleep 30")
    with pytest.raises(LocalPolicyError, match="another local shell executor"):
        LocalShell(
            tmp_path / "shell.db",
            profiles=lambda: configured,
            authorize=lambda *_: True,
            admission=lambda: None,
        )
    await service.close()
    fresh = LocalShell(
        tmp_path / "shell.db",
        profiles=lambda: configured,
        authorize=lambda alias, sid, cwd: bindings.contains(sid, cwd),
        admission=lambda: None,
    )
    try:
        result = fresh.status(row["id"], actor=ACTOR)
        assert result["state"] == "cancelled" and result["cleanup_verified"]
    finally:
        await fresh.close()
