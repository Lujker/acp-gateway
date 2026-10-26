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
import json
import secrets
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from functools import partial

from acp_gateway.agents import AgentClient, AgentError, SessionBusy, SessionNotFound
from acp_gateway.agents.client import PermissionRequest
from acp_gateway.agents.events import MessageChunk, SessionInfoUpdated, TurnFinished
from acp_gateway.agents.model_control import identifier
from acp_gateway.channels.base import Channel
from acp_gateway.config import PolicySettings, RateLimitSettings, RetentionSettings
from acp_gateway.core.approvals import Approval, ApprovalManager
from acp_gateway.core.bus import EventBus, JobFinished, JobProgress, JobStarted, SessionCreated
from acp_gateway.core.errors import (
    GatewayError,
    JobNotFound,
    MaintenanceBlocked,
    PolicyDenied,
    SelectionChanged,
    UnknownAgent,
    UnknownSession,
)
from acp_gateway.core.policy import Policy
from acp_gateway.core.rate_limits import WorkRateLimiter
from acp_gateway.log import get_logger, redact_text
from acp_gateway.storage import Conversation, Job, JobStatus, SessionRecord, Store
from acp_gateway.storage.records import utcnow

DEFAULT_JOB_RETENTION = timedelta(days=7)
DEFAULT_CANCEL_GRACE = 10.0


def _new_job_id() -> str:
    return secrets.token_hex(6)


def _prompt_title(text: str, limit: int = 60) -> str | None:
    """A dialogue name from the first prompt line, without secrets."""
    line = next((line for line in text.splitlines() if line.strip()), "")
    line = " ".join(redact_text(line).split())
    if not line:
        return None
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


# A completed turn that the agent cut short: say so instead of an empty result.
STOPPED_EARLY = {
    "max_turn_requests": "the agent stopped at its step limit before finishing; "
    "send a follow-up message to continue",
    "max_tokens": "the agent stopped at its output limit; send a follow-up message to continue",
}


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
        job_retention: timedelta | None = None,
        retention: RetentionSettings | None = None,
        rate_limits: RateLimitSettings | None = None,
        cancel_grace: float = DEFAULT_CANCEL_GRACE,
        policy: PolicySettings | None = None,
    ) -> None:
        self.store = store
        self.bus = bus or EventBus()
        self._agents = dict(agents)
        self.work_rate_limiter = WorkRateLimiter(rate_limits)
        self._retention = retention or RetentionSettings()
        self._job_retention = (
            job_retention
            if job_retention is not None
            else timedelta(days=self._retention.jobs_days)
        )
        self._retention_task: asyncio.Task[None] | None = None
        self._cancel_grace = cancel_grace
        self._channels: dict[str, Channel] = {}
        self._running: dict[str, _RunningJob] = {}
        self._busy: dict[int, str] = {}  # session row id -> running job id
        self._locks: defaultdict[Conversation, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._log = get_logger(__name__)
        self._closed = False
        self._creating = 0
        self._model_operations = 0
        self._model_confirmations: dict[Conversation, tuple] = {}
        self._maintenance_token: str | None = None
        self.policy = Policy(policy)
        self.approvals = ApprovalManager(store, self.bus, self.policy, lambda: self._channels)
        for alias, client in self._agents.items():
            client.set_permission_handler(partial(self._on_permission, alias))

    # --------------------------------------------------------------- lifecycle

    @property
    def agents(self) -> Mapping[str, AgentClient]:
        return self._agents

    def add_agent(self, client: AgentClient) -> None:
        """Publish a newly discovered route to all running channels."""
        alias = client.profile.address
        if self._closed or alias in self._agents:
            raise ValueError("agent route is already registered or gateway is closed")
        client.set_permission_handler(partial(self._on_permission, alias))
        self._agents[alias] = client
        for channel in self._channels.values():
            callback = getattr(channel, "agent_added", None)
            if callback is not None:
                callback(alias)

    async def remove_agents(self, aliases) -> None:
        clients = []
        for alias in aliases:
            clients.append(self._agents.pop(alias))
            for channel in self._channels.values():
                callback = getattr(channel, "agent_removed", None)
                if callback is not None:
                    callback(alias)
        await asyncio.gather(*(client.close() for client in clients))

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
        self.store.models.recover_intents()
        interrupted = self.store.interrupt_running_jobs()
        self._sweep_retention()
        self._log.info("gateway core started", interrupted_jobs=interrupted)
        if self._retention_task is None:
            self._retention_task = asyncio.create_task(
                self._run_retention(), name="gateway-retention"
            )
        for channel in self._channels.values():
            await channel.start(self)

    def _sweep_retention(self) -> None:
        try:
            now = utcnow()
            jobs = self.store.prune_jobs(
                now - self._job_retention, limit=self._retention.batch_size
            )
            approvals = 0
            if self._retention.approval_audit_days is not None:
                approvals = self.store.prune_approval_audit(
                    now - timedelta(days=self._retention.approval_audit_days),
                    limit=self._retention.batch_size,
                )
            self._log.info(
                "retention sweep completed", pruned_jobs=jobs, pruned_approvals=approvals
            )
        except Exception:
            # A cleanup failure must not prevent serving or the next scheduled attempt.
            # Avoid SQLite exception text, which can contain database paths.
            self._log.warning("retention sweep failed; will retry on next interval")

    async def _run_retention(self) -> None:
        while True:
            await asyncio.sleep(self._retention.sweep_interval_seconds)
            self._sweep_retention()

    async def close(self) -> None:
        """Stop channels, interrupt running jobs, close agent connections and the store."""
        if self._closed:
            return
        self._closed = True
        if self._retention_task is not None:
            self._retention_task.cancel()
            await asyncio.gather(self._retention_task, return_exceptions=True)
            self._retention_task = None
        self._model_confirmations.clear()
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

    def maintenance_status(self) -> dict:
        return {
            "reserved": self._maintenance_token is not None,
            "running_jobs": len(self._running),
            "creating_sessions": self._creating,
            "pending_approvals": self.approvals.pending_count,
            "model_operations": self._model_operations,
            "unresolved_model_states": self.store.models.unresolved_count(),
        }

    def prepare_maintenance(self) -> str:
        """Atomically reserve an idle event-loop core; no model work is cancelled."""
        self._check_admission()
        if (
            self._running
            or self._creating
            or self.approvals.pending_count
            or self._model_operations
            or self.store.models.unresolved_count()
        ):
            raise MaintenanceBlocked(
                "Finish active jobs, approvals, session creation and model operations "
                "before maintenance."
            )
        self._maintenance_token = secrets.token_hex(16)
        return self._maintenance_token

    def resume_maintenance(self, token: str | None = None, *, force: bool = False) -> None:
        if not force and (
            self._maintenance_token is None
            or not isinstance(token, str)
            or not token.isascii()
            or not secrets.compare_digest(self._maintenance_token, token)
        ):
            raise MaintenanceBlocked("The maintenance reservation does not match this process.")
        self._maintenance_token = None

    def _check_admission(self) -> None:
        if self._closed:
            raise GatewayError("the gateway has stopped")
        if self._maintenance_token is not None:
            raise MaintenanceBlocked(
                "The dispatcher is reserved for maintenance; retry after resume."
            )

    def sessions(self, conversation: Conversation) -> list[SessionRecord]:
        self.agent(conversation.agent)
        return [self._session_view(record) for record in self.store.sessions(conversation)]

    def _recent_view(self, record: SessionRecord) -> SessionRecord:
        if record.conversation.agent in self.agents:
            return self._session_view(record)
        title = self.store.channel_state(
            "dialogue_display_titles", self._dialogue_title_key(record), record.title
        )
        return replace(record, availability="agent-unavailable", title=title)

    def recent_dialogues(self, channel: str, key: str, *, limit: int = 10) -> list[SessionRecord]:
        Conversation(channel, key, "recent")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise GatewayError("Invalid recent dialogue limit.")
        return [
            self._recent_view(record) for record in self.store.recent_sessions(channel, key, limit)
        ]

    def owned_dialogue(self, dialogue_id: int, *, channel: str, key: str) -> SessionRecord:
        record = self.store.session(dialogue_id) if type(dialogue_id) is int else None
        if (
            record is None
            or record.conversation.channel != channel
            or record.conversation.key != key
        ):
            raise UnknownSession("This chat does not own that dialogue.")
        return self._recent_view(record)

    def _session_view(self, record: SessionRecord) -> SessionRecord:
        client = self.agent(record.conversation.agent)
        remember = getattr(client, "remember_session_binding", None)
        if remember is not None and record.binding is not None:
            remember(record.acp_session_id, record.binding)
        availability = getattr(client, "session_availability", lambda sid: "unknown")
        state = availability(record.acp_session_id)
        model_state = self.store.models.state(record.id)
        if model_state and model_state["state"] in {"intent", "unknown"}:
            state = "uncertain"
        if record.id in self._busy and state in {"unknown", "attached"}:
            state = "busy"
        title = self.store.channel_state(
            "dialogue_display_titles", self._dialogue_title_key(record), record.title
        )
        return replace(record, availability=state, title=title)

    def active_session(self, conversation: Conversation) -> SessionRecord | None:
        self.agent(conversation.agent)
        record = self.store.active_session(conversation)
        return self._session_view(record) if record else None

    async def new_session(
        self, conversation: Conversation, cwd: str | None = None
    ) -> SessionRecord:
        """Create an agent session and make it the conversation's active session."""
        self._check_admission()
        self.agent(conversation.agent)
        self.policy.check_new_session()
        self.work_rate_limiter.check(conversation.channel)
        async with self._locks[conversation]:
            return await self._create_session(conversation, cwd)

    def switch_session(self, conversation: Conversation, acp_session_id: str) -> SessionRecord:
        """Make one of the conversation's own sessions active."""
        self.agent(conversation.agent)
        record = self.store.find_session(conversation, acp_session_id)
        if record is None:
            raise UnknownSession(f"this conversation has no session {acp_session_id}")
        self.store.set_active(conversation, record.id)
        return self._session_view(record)

    def switch_dialogue(self, conversation: Conversation, dialogue_id: int) -> SessionRecord:
        """Select an owned persistent row ID, independent of backend handles.

        Entry points delegate ownership and selection to the core rather than
        implementing their own storage checks. This is not account authorization;
        the conversation is still supplied by a trusted channel adapter.
        """
        self.agent(conversation.agent)
        record = self.store.session(dialogue_id) if type(dialogue_id) is int else None
        if record is None or record.conversation != conversation:
            raise UnknownSession("This conversation does not own that dialogue.")
        self.store.set_active(conversation, record.id)
        return self._session_view(record)

    @staticmethod
    def _dialogue_title_key(record: SessionRecord) -> str:
        return json.dumps([str(record.conversation), record.id, record.created_at.isoformat()])

    def rename_dialogue(
        self, conversation: Conversation, dialogue_id: int, title: str
    ) -> SessionRecord:
        """Set a dispatcher display name without changing the agent's title."""
        self._check_admission()
        self.agent(conversation.agent)
        record = self.store.session(dialogue_id) if type(dialogue_id) is int else None
        if record is None or record.conversation != conversation:
            raise UnknownSession("This conversation does not own that dialogue.")
        title = " ".join(title.split())
        if not title or len(title) > 120 or not title.isprintable():
            raise GatewayError("Use a title of 1-120 printable characters.")
        self.store.set_channel_state(
            "dialogue_display_titles", self._dialogue_title_key(record), title
        )
        return self._session_view(record)

    async def _create_session(
        self, conversation: Conversation, cwd: str | None = None
    ) -> SessionRecord:
        self._check_admission()
        client = self.agent(conversation.agent)
        cwd = cwd or client.profile.default_cwd
        self.policy.check_new_session()
        revision = self.store.active_revision(conversation)
        self._creating += 1
        try:
            acp_session_id = await client.new_session(cwd)
        finally:
            self._creating -= 1
        if self._closed:
            raise GatewayError("the gateway has stopped")
        binding = getattr(client, "session_binding", lambda sid: None)(acp_session_id)
        default = None
        if client.profile.kind == "hermes":
            if binding:
                self.store.set_channel_state(
                    "hermes_route_profile", conversation.agent, binding["profile"]
                )
            default = self.model_default(conversation)["selection"]
        record = self.store.add_session(
            conversation,
            acp_session_id,
            cwd or client.profile.default_cwd,
            binding=binding,
            model_default_required=default,
            expected_active_revision=revision,
        )
        record = self._session_view(record)
        self._log.info("session created", conversation=str(conversation), session_id=acp_session_id)
        self.bus.publish(SessionCreated(conversation, record))
        active = self.store.active_session(conversation)
        if active is None or active.id != record.id:
            raise GatewayError(
                f"Session {record.id} was created, but your active selection changed meanwhile. "
                f"It was kept; use /switch {record.id} to select the created session."
            )
        if default is not None:
            # Retain the new dialogue on any failure; never prompt on an unintended model.
            try:
                result = await self._model_operation(
                    conversation,
                    record.id,
                    self.store.active_revision(conversation),
                    selection={**default, "confirmation_token": None},
                    locked=True,
                )
            except (AgentError, GatewayError) as exc:
                raise GatewayError(
                    f"Session {record.id} was created, but its model default needs attention; "
                    "use model state/show/set for this dialogue before prompting."
                ) from exc
            if result["status"] == "confirmation_required":
                raise GatewayError(
                    f"Session {record.id} needs model confirmation. "
                    "Repeat model set explicitly to obtain a fresh confirmation token."
                )
        return record

    def _model_default_scope(self, conversation: Conversation) -> str:
        profile = self.agent(conversation.agent).profile
        if profile.kind != "hermes":
            raise GatewayError("Dispatcher model defaults require a Hermes agent.")
        # These identities are assigned by the application, never accepted from channels.
        if profile.backend == "connector":
            hermes_profile = self.store.channel_state("hermes_route_profile", conversation.agent)
            if hermes_profile is None:
                raise GatewayError(
                    "Create a dialogue first to identify this connector's Hermes profile."
                )
        else:
            hermes_profile = profile.hermes.profile if profile.hermes else "default"
        return json.dumps(
            [
                "single-owner",
                "default-workspace",
                conversation.agent,
                profile.computer_id,
                profile.alias,
                hermes_profile,
            ],
            separators=(",", ":"),
        )

    def model_default(self, conversation: Conversation) -> dict:
        scope = self._model_default_scope(conversation)
        return {
            "agent": conversation.agent,
            "selection": self.store.channel_state("model_default", scope),
        }

    def set_model_default(self, conversation: Conversation, *, provider: str, model: str) -> dict:
        self._check_admission()
        try:
            selection = {"provider": identifier(provider), "model": identifier(model)}
        except ValueError:
            raise GatewayError("Invalid provider or model identifier.") from None
        scope = self._model_default_scope(conversation)
        self.store.set_channel_state("model_default", scope, selection)
        return self.model_default(conversation)

    def clear_model_default(self, conversation: Conversation) -> dict:
        self._check_admission()
        self.store.set_channel_state("model_default", self._model_default_scope(conversation), None)
        return self.model_default(conversation)

    def model_state(self, conversation: Conversation, dialogue_id: int) -> dict:
        """Read durable evidence without reconnecting or interpreting it as applied."""
        self.agent(conversation.agent)
        record = self.store.session(dialogue_id)
        if record is None or record.conversation != conversation:
            raise UnknownSession("This conversation does not own that dialogue.")
        return {
            "dialogue_id": record.id,
            "model_state": self.store.models.state(record.id),
            "default_required": self.store.channel_state("model_default_required", str(record.id)),
        }

    async def model_options(
        self,
        conversation: Conversation,
        *,
        dialogue_id: int | None = None,
        expected_revision: int | None = None,
        admission=None,
    ) -> dict:
        return await self._model_operation(
            conversation, dialogue_id, expected_revision, guard=admission
        )

    async def set_model(
        self,
        conversation: Conversation,
        *,
        provider: str,
        model: str,
        dialogue_id: int | None = None,
        expected_revision: int | None = None,
        confirmation_token: str | None = None,
        admission=None,
    ) -> dict:
        return await self._model_operation(
            conversation,
            dialogue_id,
            expected_revision,
            guard=admission,
            selection={
                "provider": provider,
                "model": model,
                "confirmation_token": confirmation_token,
            },
        )

    async def _model_operation(
        self,
        conversation,
        dialogue_id,
        expected_revision,
        *,
        selection=None,
        locked=False,
        guard=None,
    ):
        self._check_admission()
        client = self.agent(conversation.agent)
        async with contextlib.nullcontext() if locked else self._locks[conversation]:
            self._check_admission()
            record = self.store.active_session(conversation)
            if record is None:
                raise UnknownSession("No active dialogue; select or create one explicitly.")
            if dialogue_id is not None and (
                type(dialogue_id) is not int or record.id != dialogue_id
            ):
                raise UnknownSession("The selected conversation does not own this active dialogue.")
            if record.id in self._busy:
                raise SessionBusy("Finish the current job before changing model settings.")
            revision = self.store.active_revision(conversation)
            if expected_revision is not None and (
                type(expected_revision) is not int or expected_revision != revision
            ):
                raise SelectionChanged("Dialogue selection changed; refresh before retrying.")
            durable = self.store.models.state(record.id)
            if (
                selection is not None
                and durable
                and durable["state"] in {"intent", "pending", "unknown"}
            ):
                raise SessionBusy("Model selection is pending or unknown; no automatic retry")
            confirmation = None
            if selection is not None and selection.get("confirmation_token") is not None:
                confirmation = (
                    selection["confirmation_token"],
                    record.id,
                    selection["provider"],
                    selection["model"],
                    revision,
                )
                if self._model_confirmations.get(conversation) != confirmation:
                    raise SelectionChanged("Model confirmation belongs to an earlier selection.")
            self._session_view(record)

            sent = False
            operation_id = secrets.token_hex(16) if selection is not None else None

            def admission():
                nonlocal sent
                self._check_admission()
                if guard is not None:
                    guard()
                active = self.store.active_session(conversation)
                if (
                    active is None
                    or active.id != record.id
                    or self.store.active_revision(conversation) != revision
                ):
                    raise SelectionChanged("Dialogue selection changed before the model operation.")
                if selection is not None:
                    if (
                        confirmation is not None
                        and self._model_confirmations.get(conversation) != confirmation
                    ):
                        raise SelectionChanged("Model confirmation is no longer current.")
                    self._model_confirmations.pop(conversation, None)
                    self.store.models.begin(
                        record.id,
                        selection["provider"],
                        selection["model"],
                        revision,
                        operation_id=operation_id,
                    )
                    sent = True

            self._model_operations += 1
            try:
                if selection is None:
                    result = await client.model_options(record.acp_session_id, admission=admission)
                else:
                    result = await client.set_model(
                        record.acp_session_id,
                        admission=admission,
                        operation_id=operation_id,
                        **selection,
                    )
                if selection is None:
                    self.store.models.observe(record.id, result)
                else:
                    status = result["status"]
                    self.store.models.settle(
                        record.id,
                        "confirmation_required" if status == "confirmation_required" else "pending",
                        deferred=status == "deferred",
                        resolved_request=result.get("resolved_request"),
                    )
                    if status in {"accepted", "deferred"}:
                        self.store.set_channel_state("model_default_required", str(record.id), None)
                changed = self.store.active_revision(conversation) != revision
                if result.get("status") == "confirmation_required" and not changed:
                    if len(self._model_confirmations) >= 1024:
                        self._model_confirmations.pop(next(iter(self._model_confirmations)))
                    self._model_confirmations[conversation] = (
                        result["confirmation_token"],
                        record.id,
                        selection["provider"],
                        selection["model"],
                        revision,
                    )
                return {
                    "dialogue_id": record.id,
                    "selection_revision": revision,
                    "selection_changed": changed,
                    "model_state": self.store.models.state(record.id),
                    **result,
                }
            except BaseException as exc:
                if sent:
                    from acp import RequestError

                    from acp_gateway.agents.model_control import MODEL_REFUSED

                    cause = exc.__cause__
                    refused = isinstance(cause, RequestError) and cause.code in {
                        MODEL_REFUSED,
                        -32601,
                    }
                    self.store.models.settle(record.id, "refused" if refused else "unknown")
                raise
            finally:
                self._model_operations -= 1

    async def reconcile_model(
        self,
        conversation: Conversation,
        *,
        dialogue_id: int,
        expected_revision: int,
        admission=None,
    ) -> dict:
        self._check_admission()
        if (
            type(dialogue_id) is not int
            or type(expected_revision) is not int
            or expected_revision < 0
        ):
            raise SelectionChanged("Reconciliation requires an explicit dialogue and revision.")
        client = self.agent(conversation.agent)
        async with self._locks[conversation]:
            record = self.store.active_session(conversation)
            if record is None or record.id != dialogue_id:
                raise UnknownSession("Select the owned dialogue before reconciling its model.")
            self._session_view(record)

            def guard():
                self._check_admission()
                if admission is not None:
                    admission()
                active = self.store.active_session(conversation)
                if (
                    active is None
                    or active.id != record.id
                    or self.store.active_revision(conversation) != expected_revision
                ):
                    raise SelectionChanged("Dialogue selection changed before reconciliation.")

            guard()
            if record.id in self._busy:
                raise SessionBusy("Finish the current job before model reconciliation.")
            state = self.store.models.state(record.id)
            operation = self.store.channel_state("model_operation_id", str(record.id))
            self._model_operations += 1
            try:
                if state and state["state"] in {"intent", "unknown"}:
                    outcome = (
                        await client.recover_model(
                            record.acp_session_id, operation, admission=guard
                        )
                        if operation
                        else {"status": "unavailable"}
                    )
                    if outcome["status"] in {"accepted", "deferred"}:
                        self.store.models.settle(
                            record.id,
                            "pending",
                            deferred=outcome["status"] == "deferred",
                            resolved_request=outcome.get("resolved_request"),
                        )
                        self.store.set_channel_state("model_default_required", str(record.id), None)
                    elif outcome["status"] == "not_applied":
                        self.store.models.settle(record.id, "refused")
                    else:
                        return {
                            "dialogue_id": record.id,
                            "selection_revision": expected_revision,
                            "selection_changed": self.store.active_revision(conversation)
                            != expected_revision,
                            "reconciliation": outcome["status"],
                            "model_state": self.store.models.state(record.id),
                        }
                # A fresh observation is still required after a known ACK.
                result = await client.model_options(record.acp_session_id, admission=guard)
                self.store.models.observe(record.id, result)
                return {
                    "dialogue_id": record.id,
                    "selection_revision": expected_revision,
                    "selection_changed": self.store.active_revision(conversation)
                    != expected_revision,
                    "reconciliation": self.store.models.state(record.id)["state"],
                    "model_state": self.store.models.state(record.id),
                    **result,
                }
            finally:
                self._model_operations -= 1

    # -------------------------------------------------------------------- jobs

    async def submit_selected(
        self, conversation: Conversation, text: str, *, delivery_namespace: str | None = None
    ) -> Job:
        """Send to the persistent selection, without implicit session creation.

        Shared by interactive entry points; selection and job creation happen
        together under the conversation lock. Legacy programmatic submit keeps
        its existing auto-create contract until callers explicitly opt in.
        """
        return await self.submit(
            conversation, text, create_session=False, delivery_namespace=delivery_namespace
        )

    async def submit(
        self,
        conversation: Conversation,
        text: str,
        *,
        acp_session_id: str | None = None,
        create_session: bool = True,
        delivery_namespace: str | None = None,
    ) -> Job:
        """Start a turn in the active or explicitly selected owned session.

        Returns the running job at once; raises ``SessionBusy`` if the session
        is already running a job, and agent errors if a session cannot be created.
        An explicit session leaves the active pointer unchanged.
        """
        self._check_admission()
        client = self.agent(conversation.agent)
        self.policy.check_prompt(text)
        self.work_rate_limiter.check(conversation.channel)
        async with self._locks[conversation]:
            self._check_admission()
            if acp_session_id is not None:
                session = self.store.find_session(conversation, acp_session_id)
                if session is None:
                    raise UnknownSession("this conversation does not own that session")
            else:
                session = self.store.active_session(conversation)
            if session is None:
                if not create_session:
                    raise UnknownSession("No active dialogue; use /new or /switch SESSION_ID.")
                session = await self._create_session(conversation)
            if session.id in self._busy:
                raise SessionBusy(
                    f"{client.profile.display_name} is still working on the previous request"
                    " in this session"
                )
            if self.store.channel_state("model_default_required", str(session.id)):
                raise SessionBusy(
                    "This dialogue needs its model default selected or confirmed first."
                )
            durable = self.store.models.state(session.id)
            if durable and durable["state"] in {"intent", "unknown"}:
                raise SessionBusy("Model selection outcome is unknown; no automatic retry")
            state = self._session_view(session).availability
            if state == "restore-unavailable" or (
                state == "uncertain"
                and session.binding
                and session.binding.get("restore") == "disabled"
            ):
                raise SessionNotFound(
                    "This Hermes dialogue remains recorded but cannot safely be restored; "
                    "create a new session with /new. The previous prompt will not be replayed."
                )
            job = self.store.add_job(
                _new_job_id(), session.id, delivery_namespace=delivery_namespace
            )
            self._model_confirmations.pop(conversation, None)
            self.store.touch_session(session.id)
            if not session.title and (title := _prompt_title(text)):
                # Managed Codex/Claude Code do not name dialogues; an agent title wins later.
                self.store.set_session_title(session.id, title)
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

    def owned_job(self, job_id: str, *, channel: str, key: str) -> Job:
        job = self.job(job_id)
        if job.conversation.channel != channel or job.conversation.key != key:
            raise JobNotFound("This chat does not own that job.")
        return job

    def job_history(
        self, channel: str, key: str, *, before=None, limit=10, active=False, agent=None
    ) -> dict:
        Conversation(channel, key, agent or "history")
        if type(limit) is not int or not 1 <= limit <= 50 or type(active) is not bool:
            raise GatewayError("Invalid job history options.")
        cursor = None
        if before is not None:
            cursor = self.store.job_history_cursor(before, channel, key, agent)
            if cursor is None:
                raise JobNotFound("History cursor not found in this scope; refresh the list.")
        rows = self.store.job_history(
            channel, key, before=cursor, limit=limit + 1, active=active, agent=agent
        )
        return {
            "jobs": rows[:limit],
            "next_before": rows[limit - 1].id if len(rows) > limit else None,
        }

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
        if not getattr(self.agent(run.job.conversation.agent), "supports_cancel", True):
            raise AgentError("safe turn cancellation is unavailable for this agent")
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
                error = run.notice or STOPPED_EARLY.get(finished.stop_reason)
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
                if status is JobStatus.COMPLETED:
                    self.store.models.turn_finished(job.session_id)
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

    def execution_mode(self, conversation: Conversation) -> dict:
        record = self.store.active_session(conversation)
        if record is None:
            raise UnknownSession("Select or create a dialogue first.")
        saved = self.store.channel_state("execution_mode", str(record.id), {})
        return {
            "dialogue_id": record.id,
            "mode": saved.get("mode", "ask")
            if (
                saved.get("session") == record.acp_session_id
                and saved.get("agent") == conversation.agent
                and saved.get("channel") == conversation.channel
                and saved.get("actor") == f"{conversation.channel}:{conversation.key}"
            )
            else "ask",
            "selection_revision": self.store.active_revision(conversation),
        }

    def set_execution_mode(
        self,
        conversation: Conversation,
        mode: str,
        *,
        dialogue_id: int,
        expected_revision: int,
        actor: str,
    ) -> dict:
        """Explicit human authorization for one dialogue's future permission requests.

        This never changes the host profile or allows requests belonging to other
        channels/dialogues. New dialogues always use upstream/human approvals.
        """
        self._check_admission()
        channel = self._channels.get(conversation.channel)
        if (
            channel is None
            or not self.policy.can_approve(channel)
            or conversation.channel != "telegram"
            or actor != f"telegram:{conversation.key}"
        ):
            raise PolicyDenied("Execution mode requires this chat's authenticated approver.")
        if mode not in {"ask", "auto"}:
            raise GatewayError("Choose execution mode ask or auto.")
        state = self.execution_mode(conversation)
        if (
            type(dialogue_id) is not int
            or type(expected_revision) is not int
            or state["dialogue_id"] != dialogue_id
            or state["selection_revision"] != expected_revision
        ):
            raise SelectionChanged("Dialogue changed; refresh execution settings.")
        if dialogue_id in self._busy or self._locks[conversation].locked():
            raise SessionBusy("Finish the current operation before changing execution mode.")
        self.store.set_channel_state(
            "execution_mode",
            str(dialogue_id),
            {
                "mode": mode,
                "session": self.store.active_session(conversation).acp_session_id,
                "agent": conversation.agent,
                "actor": actor,
                "channel": conversation.channel,
                "changed_at": utcnow().isoformat(),
            },
        )
        self._log.info(
            "dialogue execution mode changed", dialogue_id=dialogue_id, mode=mode, actor=actor
        )
        return self.execution_mode(conversation)

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
        authorization = self.store.channel_state("execution_mode", str(record.job.session_id), {})
        # Match the originating private chat, never the currently selected dialogue.
        if not (
            authorization.get("mode") == "auto"
            and authorization.get("session") == record.job.acp_session_id
            and authorization.get("agent") == record.job.conversation.agent
            and record.job.conversation.channel == "telegram"
            and authorization.get("channel") == "telegram"
            and authorization.get("actor") == f"telegram:{record.job.conversation.key}"
        ):
            authorization = None
        result = await self.approvals.request(
            record.job.conversation, record.job.id, request, automatic=authorization
        )
        if result.reason and result.outcome != "approved":
            record.notice = result.reason
        return result.option_id

    def pending_approvals(self, channel: str) -> list[Approval]:
        return self.approvals.pending(channel)

    def resolve_approval(
        self, approval_id: str, option_id: str, *, channel: str, actor: str
    ) -> None:
        self.approvals.resolve(approval_id, option_id, channel=channel, actor=actor)
