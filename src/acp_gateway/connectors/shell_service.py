"""Local work-profile shell admission, durable ownership and bounded task lifecycle."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from filelock import FileLock, Timeout

from acp_gateway.agents.errors import AgentError
from acp_gateway.connectors.policy import LocalAgentPolicy, LocalPolicyError
from acp_gateway.connectors.protocol import ShellResult
from acp_gateway.connectors.shell_journal import ShellJournal, identity
from acp_gateway.connectors.shell_runner import ShellRunner
from acp_gateway.log import get_logger


class LocalShell:
    def __init__(self, path, *, profiles, authorize, admission, runner_factory=ShellRunner):
        self.profiles, self.authorize, self.admission = profiles, authorize, admission
        self.runner_factory = runner_factory
        self.journal = ShellJournal(path)
        self.lock = FileLock(Path(path).with_suffix(".lock"), mode=0o600, thread_local=False)
        try:
            self.lock.acquire(timeout=0)
        except Timeout:
            self.journal.close()
            raise LocalPolicyError("another local shell executor owns this journal") from None
        try:
            self.journal.recover()
            self.journal.expire(before=time.time() - 7 * 86400)
        except BaseException:
            self.lock.release()
            self.journal.close()
            raise
        self.tasks, self.cancellation = {}, {}
        self.closed = False

    def start(self, operation, *, actor, alias, session_id, cwd, command, timeout=120):
        if self.closed:
            raise LocalPolicyError("local shell executor is closed")
        loop = asyncio.get_running_loop()
        self.admission()
        profile = next((p for p in self.profiles() if p.alias == alias), None)
        if profile is None:
            raise LocalPolicyError("local shell profile unavailable")
        if profile.kind not in {"codex", "claude_code"} or profile.backend != profile.kind:
            raise LocalPolicyError("local shell profile is not managed")
        runner = self.runner_factory(profile)
        if not runner.supported():
            raise LocalPolicyError("shell supervisor unavailable for this profile platform")
        cwd = LocalAgentPolicy.from_profile(profile).prepare(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/list",
                "params": {"cwd": cwd},
            }
        )["params"]["cwd"]
        if not self.authorize(alias, session_id, cwd):
            raise LocalPolicyError("shell dialogue is not owned by this local profile")
        self.journal.expire(before=time.time() - 7 * 86400)
        created, row = self.journal.reserve(
            operation,
            actor=actor,
            alias=alias,
            session_id=session_id,
            cwd=cwd,
            command=command,
            timeout=timeout,
        )
        if created:
            key = row["id"]
            self.journal.running(operation, actor=actor, interpreter=runner.interpreter)
            event = asyncio.Event()
            self.cancellation[key] = event
            task = loop.create_task(
                self._execute(key, actor, runner, command, cwd, timeout, event),
                name="shell-" + key,
            )
            self.tasks[key] = task
            task.add_done_callback(lambda completed: self._done(key, completed))
        return self.status(operation, actor=actor)

    async def request(self, request):
        """A bounded control RPC: enqueue a task, never await command completion."""
        if not time.time() <= request.expires <= time.time() + 120:
            return ShellResult(operation=request.operation, state="refused", reason="stale")
        try:
            if request.action == "start":
                row = self.start(
                    request.operation,
                    actor=request.actor,
                    alias=request.alias,
                    session_id=request.session_id,
                    cwd=request.cwd,
                    command=request.command,
                    timeout=request.timeout,
                )
            elif request.action == "cancel":
                row = self.cancel(request.operation, actor=request.actor)
            else:
                row = self.status(request.operation, actor=request.actor)
        except AgentError as exc:
            # Only fixed local-policy messages map to public codes; raw input,
            # host exceptions, command text and paths never become a reason.
            reasons = {
                "Connector reserved for maintenance": "maintenance",
                "local shell executor is closed": "disabled",
                "local shell profile unavailable": "profile_unavailable",
                "local shell profile is not managed": "profile_unavailable",
                "direct shell requires a local managed work profile": "safe_profile",
                "shell supervisor unavailable for this profile platform": "unsupported_platform",
                "shell dialogue is not owned by this local profile": "session_unavailable",
                "cwd is not allowed by local policy": "cwd_not_allowed",
                "shell concurrency limit reached": "busy",
                "shell operation journal capacity reached": "journal_full",
                "shell result storage capacity reached": "journal_full",
                "shell operation unavailable for this actor": "not_found",
            }
            return ShellResult(
                operation=request.operation,
                state="refused",
                reason=reasons.get(str(exc), "invalid"),
            )
        if row["state"] in {"reserved", "running", "expired"}:
            return ShellResult(
                operation=request.operation, state=row["state"], interpreter=row["shell"]
            )
        output = row["output"]
        if request.offset > len(output):
            return ShellResult(operation=request.operation, state="refused", reason="invalid")
        return ShellResult(
            operation=request.operation,
            state=row["state"],
            reason=row["reason"],
            interpreter=row["shell"],
            output=output[request.offset : request.offset + 4000],
            offset=request.offset,
            total=len(output),
            exit_code=row["exit_code"],
            cleanup_verified=bool(row["cleanup_verified"]),
            output_omitted=bool(row["output_omitted"]),
        )

    def status(self, operation, *, actor):
        self.journal.expire(before=time.time() - 7 * 86400)
        row = self.journal.get(operation, actor=actor)
        return {key: value for key, value in row.items() if key != "digest"}

    def cancel(self, operation, *, actor):
        row = self.status(operation, actor=actor)
        event = self.cancellation.get(identity(operation))
        if event is not None:
            event.set()
        return row

    async def _execute(self, key, actor, runner, command, cwd, timeout, event):
        try:
            if event.is_set():
                result = {"state": "cancelled", "cleanup_verified": True}
            else:
                result = await runner.run(command, cwd, timeout=timeout, cancel=event)
        except asyncio.CancelledError:
            result = {"state": "unknown", "reason": "interrupted"}
        except Exception:
            # An unexpected runner exception does not prove whether it reached spawn
            # or completed cleanup. Only the runner's verified receipt may do that.
            result = {"state": "unknown", "reason": "execution_failed"}
        self.journal.finish(key, actor=actor, **result)

    def _done(self, key, task):
        self.tasks.pop(key, None)
        self.cancellation.pop(key, None)
        try:
            task.result()
        except BaseException:
            # A storage/cleanup failure is not permission to execute the intent again.
            # A remaining running marker blocks maintenance and recovers as unknown.
            get_logger(__name__).warning(
                "shell outcome could not be persisted; inspect local state"
            )

    def maintenance_status(self):
        return self.journal.activity()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for event in self.cancellation.values():
            event.set()
        pending = set(self.tasks.values())
        try:
            if pending:
                _, remaining = await asyncio.wait(pending, timeout=10)
                for task in remaining:
                    task.cancel()
                if remaining:
                    await asyncio.gather(*remaining, return_exceptions=True)
        finally:
            try:
                self.journal.recover()
            finally:
                self.journal.close()
                self.lock.release()
