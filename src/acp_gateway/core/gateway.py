"""GatewayCore: sessions, jobs and the event bus shared by every channel.

- A conversation (``channel``, ``key``, ``agent``) owns any number of agent
  sessions and points at one active session; the mapping lives in SQLite and
  survives restarts.
- Every prompt is a job: :meth:`GatewayCore.submit` starts it and returns at
  once, :meth:`GatewayCore.wait` waits for it with a timeout. A session runs one
  job at a time; a second prompt is refused with ``SessionBusy``.
- Agent events of a running job, job start and job end are published on
  :attr:`GatewayCore.bus`.

Approval routing, audit and policy apply to every channel through the core.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from functools import partial

from acp_gateway.agents import AgentClient, AgentError, SessionBusy
from acp_gateway.agents.client import PermissionRequest
from acp_gateway.agents.events import MessageChunk, SessionInfoUpdated, TurnFinished
from acp_gateway.channels.base import Channel
from acp_gateway.config import PolicySettings
from acp_gateway.core.approvals import Approval, ApprovalManager
from acp_gateway.core.bus import EventBus, JobFinished, JobProgress, JobStarted, SessionCreated
from acp_gateway.core.errors import (
    GatewayError,
    JobNotFound,
    PolicyDenied,
    UnknownAgent,
    UnknownSession,
)
from acp_gateway.core.policy import Policy
from acp_gateway.log import get_logger
from acp_gateway.storage import Conversation, Job, JobStatus, SessionRecord, Store
from acp_gateway.storage.records import utcnow

DEFAULT_JOB_RETENTION = timedelta(days=7)
DEFAULT_CANCEL_GRACE = 10.0


def _new_job_id() -> str:
    return secrets.token_hex(6)


class _RunningJob:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.parts: list[str] = []
        self.answer_chars = 0
        self.notice: str | None = None
        self.done = asyncio.Event()
        self.final: Job | None = None
        self.cancel_requested = False
        self.task: asyncio.Task[None] | None = None

    def snapshot(self) -> Job:
        return self.final or replace(self.job, answer="".join(self.parts))


class GatewayCore:
    """The in-process core; it owns the store and the agent clients and closes them."""

    def __init__(
        self,
        store: Store,
        agents: Mapping[str, AgentClient],
        *,
        bus: EventBus | None = None,
        job_retention: timedelta = DEFAULT_JOB_RETENTION,
        cancel_grace: float = DEFAULT_CANCEL_GRACE,
        policy: PolicySettings | None = None,
    ) -> None:
        self.store = store
        self.bus = bus or EventBus()
        self._agents = dict(agents)
        self._job_retention = job_retention
        self._cancel_grace = cancel_grace
        self._channels: dict[str, Channel] = {}
        self._running: dict[str, _RunningJob] = {}
        self._busy: dict[int, str] = {}  # session row id -> running job id
        self._locks: defaultdict[Conversation, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._log = get_logger(__name__)
        self._closed = False
        self.policy = Policy(policy)
        self.approvals = ApprovalManager(store, self.bus, self.policy, lambda: self._channels)
        for alias, client in self._agents.items():
            client.set_permission_handler(partial(self._on_permission, alias))

    # --------------------------------------------------------------- lifecycle

    @property
    def agents(self) -> Mapping[str, AgentClient]:
        return self._agents

    def agent(self, alias: str) -> AgentClient:
        try:
            return self._agents[alias]
        except KeyError:
            raise UnknownAgent(f"unknown agent {alias!r}") from None

    @property
    def channels(self) -> Mapping[str, Channel]:
        return self._channels

    def add_channel(self, channel: Channel) -> None:
        if channel.name in self._channels:
            raise ValueError(f"channel {channel.name!r} is already registered")
        self._channels[channel.name] = channel

    async def start(self) -> None:
        """Settle jobs left by a previous process, prune old jobs, start the channels."""
        if self._closed:
            raise GatewayError("the gateway has stopped")
        interrupted = self.store.interrupt_running_jobs()
        pruned = self.store.prune_jobs(utcnow() - self._job_retention)
        self._log.info("gateway core started", interrupted_jobs=interrupted, pruned_jobs=pruned)
        for channel in self._channels.values():
            await channel.start(self)

    async def close(self) -> None:
        """Stop channels, interrupt running jobs, close agent connections and the store."""
        if self._closed:
            return
        self._closed = True
        self.approvals.close()
        for channel in reversed(self._channels.values()):
            try:
                await channel.stop()
            except Exception:
                self._log.exception("channel failed to stop", channel=channel.name)
        tasks = [run.task for run in self._running.values() if run.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        # A task cancelled before its first step never runs its own cleanup.
        for job_id, run in list(self._running.items()):
            run.final = self.store.finish_job(
                job_id,
                JobStatus.INTERRUPTED,
                error="the gateway stopped while the job was running",
            )
            self._running.pop(job_id)
            self._busy.pop(run.job.session_id, None)
            run.done.set()
        for client in self._agents.values():
            await client.close()
        self.bus.close()
        self.store.close()

    # ---------------------------------------------------------------- sessions

    def sessions(self, conversation: Conversation) -> list[SessionRecord]:
        self.agent(conversation.agent)
        return self.store.sessions(conversation)

    def active_session(self, conversation: Conversation) -> SessionRecord | None:
        self.agent(conversation.agent)
        return self.store.active_session(conversation)

    async def new_session(
        self, conversation: Conversation, cwd: str | None = None
    ) -> SessionRecord:
        """Create an agent session and make it the conversation's active session."""
        async with self._locks[conversation]:
            return await self._create_session(conversation, cwd)

    def switch_session(self, conversation: Conversation, acp_session_id: str) -> SessionRecord:
        """Make one of the conversation's own sessions active."""
        self.agent(conversation.agent)
        record = self.store.find_session(conversation, acp_session_id)
        if record is None:
            raise UnknownSession(f"this conversation has no session {acp_session_id}")
        self.store.set_active(conversation, record.id)
        return record

    async def _create_session(
        self, conversation: Conversation, cwd: str | None = None
    ) -> SessionRecord:
        if self._closed:
            raise GatewayError("the gateway has stopped")
        client = self.agent(conversation.agent)
        self.policy.check_new_session()
        acp_session_id = await client.new_session(cwd)
        if self._closed:
            raise GatewayError("the gateway has stopped")
        record = self.store.add_session(
            conversation, acp_session_id, cwd or client.profile.default_cwd
        )
        self._log.info("session created", conversation=str(conversation), session_id=acp_session_id)
        self.bus.publish(SessionCreated(conversation, record))
        return record

    # -------------------------------------------------------------------- jobs

    async def submit(
        self, conversation: Conversation, text: str, *, acp_session_id: str | None = None
    ) -> Job:
        """Start a turn in the active or explicitly selected owned session.

        Returns the running job at once; raises ``SessionBusy`` if the session
        is already running a job, and agent errors if a session cannot be created.
        An explicit session leaves the active pointer unchanged.
        """
        if self._closed:
            raise GatewayError("the gateway has stopped")
        client = self.agent(conversation.agent)
        self.policy.check_prompt(text)
        async with self._locks[conversation]:
            if self._closed:
                raise GatewayError("the gateway has stopped")
            if acp_session_id is not None:
                session = self.store.find_session(conversation, acp_session_id)
                if session is None:
                    raise UnknownSession("this conversation does not own that session")
            else:
                session = self.store.active_session(conversation)
            if session is None:
                session = await self._create_session(conversation)
            if session.id in self._busy:
                raise SessionBusy(
                    f"{client.profile.display_name} is still working on the previous request"
                    " in this session"
                )
            job = self.store.add_job(_new_job_id(), session.id)
            self.store.touch_session(session.id)
            run = _RunningJob(job)
            self._running[job.id] = run
            self._busy[session.id] = job.id
            self.bus.publish(JobStarted(conversation, job))
            run.task = asyncio.create_task(
                self._run(run, client, text, session.cwd), name=f"job-{job.id}"
            )
        self._log.info(
            "job started", job_id=job.id, conversation=str(conversation), chars=len(text)
        )
        return job

    async def ask(self, conversation: Conversation, text: str, wait: float | None = None) -> Job:
        """Submit and wait up to ``wait`` seconds (forever if ``None``)."""
        job = await self.submit(conversation, text)
        return await self.wait(job.id, wait)

    async def wait(self, job_id: str, timeout: float | None = None) -> Job:
        """The job once it finishes, or its running snapshot after ``timeout`` seconds."""
        run = self._running.get(job_id)
        if run is None:
            return self.job(job_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(run.done.wait(), timeout)
        return run.snapshot()

    def job(self, job_id: str) -> Job:
        if (run := self._running.get(job_id)) is not None:
            return run.snapshot()
        job = self.store.job(job_id)
        if job is None:
            raise JobNotFound(f"job {job_id} not found")
        return job

    def running_jobs(self, conversation: Conversation | None = None) -> list[Job]:
        return [
            run.snapshot()
            for run in self._running.values()
            if conversation is None or run.job.conversation == conversation
        ]

    async def cancel_job(self, job_id: str) -> Job:
        """Cancel a running job and return its final state; a finished job is returned as is."""
        self.policy.check_cancel()
        run = self._running.get(job_id)
        if run is None:
            return self.job(job_id)
        run.cancel_requested = True
        self.approvals.cancel_job(job_id)
        await self.agent(run.job.conversation.agent).cancel(run.job.acp_session_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(run.done.wait()), self._cancel_grace)
        if not run.done.is_set() and run.task is not None:
            # The agent did not end the turn: stop waiting for it here.
            self._log.warning("agent did not confirm cancellation", job_id=job_id)
            run.task.cancel()
            await asyncio.wait({run.task})
        return run.snapshot()

    async def cancel(self, conversation: Conversation) -> list[Job]:
        """Cancel every running job of the conversation (``/stop``)."""
        self.policy.check_cancel()
        job_ids = [job.id for job in self.running_jobs(conversation)]
        return [await self.cancel_job(job_id) for job_id in job_ids]

    async def _run(self, run: _RunningJob, client: AgentClient, text: str, cwd: str) -> None:
        job = run.job
        finished: TurnFinished | None = None
        status, error = JobStatus.FAILED, None
        try:
            if run.cancel_requested:
                status = JobStatus.CANCELLED
                return
            # aclosing: an interrupted job must close the stream, which cancels the turn.
            async with contextlib.aclosing(
                client.prompt(job.acp_session_id, text, cwd=cwd)
            ) as events:
                async for event in events:
                    if isinstance(event, MessageChunk):
                        remaining = self.policy.settings.max_response_length - run.answer_chars
                        if len(event.text) > remaining:
                            if remaining:
                                chunk = replace(event, text=event.text[:remaining])
                                run.parts.append(chunk.text)
                                run.answer_chars += remaining
                                self.bus.publish(JobProgress(job.conversation, job.id, chunk))
                            raise PolicyDenied(
                                "response exceeds "
                                f"{self.policy.settings.max_response_length} characters"
                            )
                        run.parts.append(event.text)
                        run.answer_chars += len(event.text)
                    elif isinstance(event, SessionInfoUpdated) and event.title:
                        self.store.set_session_title(job.session_id, event.title)
                    elif isinstance(event, TurnFinished):
                        finished = event
                    self.bus.publish(JobProgress(job.conversation, job.id, event))
            if run.cancel_requested or (
                finished is not None and finished.stop_reason == "cancelled"
            ):
                status = JobStatus.CANCELLED
            elif finished is not None:
                status = JobStatus.COMPLETED
                error = run.notice
            else:
                error = "the turn ended without a result"
        except (AgentError, GatewayError) as exc:
            error = str(exc)
        except asyncio.CancelledError:
            if run.cancel_requested:
                status = JobStatus.CANCELLED
                error = "the agent did not confirm the cancellation"
            else:
                status = JobStatus.INTERRUPTED
                error = "the gateway stopped while the job was running"
            raise
        except Exception:
            self._log.exception("job failed with an internal error", job_id=job.id)
            error = "internal gateway error"
        finally:
            self.approvals.cancel_job(job.id)
            outcome = {
                "answer": "".join(run.parts),
                "stop_reason": finished.stop_reason if finished else None,
                "error": error,
                "usage": finished.usage if finished else None,
            }
            try:
                final = self.store.finish_job(job.id, status, **outcome)
            except Exception:
                # Still release the session and wake the waiters.
                self._log.exception("could not record the job result", job_id=job.id)
                final = replace(job, status=status, finished_at=utcnow(), **outcome)
            run.final = final
            self._running.pop(job.id, None)
            self._busy.pop(job.session_id, None)
            run.done.set()
            self.bus.publish(JobFinished(job.conversation, final))
            self._log.info(
                "job finished",
                job_id=job.id,
                status=final.status.value,
                stop_reason=final.stop_reason,
                error=error,
            )

    async def _on_permission(self, alias: str, request: PermissionRequest) -> str | None:
        record = next(
            (
                run
                for run in self._running.values()
                if run.job.conversation.agent == alias
                and run.job.acp_session_id == request.session_id
            ),
            None,
        )
        if record is None or record.cancel_requested:
            return None
        result = await self.approvals.request(record.job.conversation, record.job.id, request)
        if result.reason:
            record.notice = result.reason
        return result.option_id

    def pending_approvals(self, channel: str) -> list[Approval]:
        return self.approvals.pending(channel)

    def resolve_approval(
        self, approval_id: str, option_id: str, *, channel: str, actor: str
    ) -> None:
        self.approvals.resolve(approval_id, option_id, channel=channel, actor=actor)
