"""Hermes-facing MCP tools; human approvals remain in separate channels."""

from typing import Annotated, Any

from fastapi.encoders import jsonable_encoder
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, SecretStr, ValidationError

from acp_gateway.agents import AgentError
from acp_gateway.api.security import OwnerAccess
from acp_gateway.channels.base import Channel
from acp_gateway.core import GatewayCore, GatewayError, JobNotFound, UnknownSession
from acp_gateway.log import get_logger
from acp_gateway.storage import Conversation, Job

Thread = Annotated[str, Field(min_length=1, max_length=256, strict=True)]
Wait = Annotated[float, Field(ge=0, le=300, allow_inf_nan=False)]
SessionId = Annotated[int, Field(gt=0, strict=True)]


class _SafeMCP(FastMCP):
    async def call_tool(self, name, arguments):
        tools = {tool.name: tool for tool in await self.list_tools()}
        if name not in tools:
            return self._error("unknown MCP tool")
        if arguments.keys() - tools[name].inputSchema.get("properties", {}).keys():
            return self._error("unexpected tool arguments")
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, AgentError | GatewayError):
                return self._error(str(cause))
            if isinstance(cause, ValidationError):
                # SDK validation errors contain input values (including prompts).
                return self._error("invalid tool arguments; check the tool schema")
            get_logger(__name__).exception("MCP tool failed", tool=name)
            return self._error("internal gateway error")

    @staticmethod
    def _error(message):
        return CallToolResult(isError=True, content=[TextContent(type="text", text=message)])


class McpChannel(Channel):
    name = "mcp"
    can_approve = False

    def __init__(self, core: GatewayCore, token: SecretStr):
        if not token.get_secret_value():
            raise ValueError("the MCP token is required")
        self.core = core
        self._started = False
        self.server = _SafeMCP(
            "ACP Gateway",
            instructions=(
                "Use a stable explicit thread for each conversation. Running jobs return a job_id; "
                "poll result with the same thread. Human approvals require a connected "
                "CLI or Telegram approver; MCP tools cannot approve actions."
            ),
            stateless_http=True,
            json_response=True,
        )
        for alias in core.agents:
            self._register(alias)
        self.app = OwnerAccess(
            self.server.streamable_http_app(),
            token=token.get_secret_value(),
            max_body_bytes=4 * core.policy.settings.max_prompt_length + 8192,
        )

    @property
    def connected(self):
        # Stateless HTTP has no persistent client connection; this is serving readiness.
        return self._started

    async def start(self, core):
        self._started = True

    async def stop(self):
        self._started = False

    def _register(self, alias):
        core = self.core

        def conversation(thread):
            return Conversation(self.name, thread, alias)

        def owned_session(thread, session_id):
            conv = conversation(thread)
            session = core.store.session(session_id)
            if session is None or session.conversation != conv:
                raise UnknownSession("this conversation does not own that session")
            return session

        def owned_job(thread, job_id):
            job = core.job(job_id)
            if job.conversation != conversation(thread):
                raise JobNotFound("this conversation does not own that job")
            return job

        def result(job: Job):
            # No partial answers, thoughts, tool data or usage reach the LLM channel.
            return {
                "job_id": job.id,
                "session_id": job.session_id,
                "status": job.status.value,
                "answer": job.answer if job.status.finished else None,
                "stop_reason": job.stop_reason if job.status.finished else None,
                "error": job.error if job.status.finished else None,
            }

        async def status(thread: Thread = "default") -> dict[str, Any]:
            client = core.agent(alias)
            return {
                "agent": alias,
                "title": client.profile.display_name,
                "connected": client.connected,
                "running_jobs": [result(job) for job in core.running_jobs(conversation(thread))],
            }

        async def sessions(thread: Thread = "default") -> dict[str, Any]:
            conv = conversation(thread)
            active = core.active_session(conv)
            return {
                "sessions": jsonable_encoder(core.sessions(conv)),
                "active_session_id": active.id if active else None,
            }

        async def new_session(thread: Thread = "default", cwd: str | None = None) -> dict[str, Any]:
            return jsonable_encoder(await core.new_session(conversation(thread), cwd))

        async def ask(
            text: Annotated[str, Field(min_length=1, strict=True)],
            thread: Thread = "default",
            wait: Wait = 30,
            session_id: SessionId | None = None,
        ) -> dict[str, Any]:
            selected = owned_session(thread, session_id) if session_id is not None else None
            job = await core.submit(
                conversation(thread),
                text,
                acp_session_id=selected.acp_session_id if selected else None,
            )
            return result(await core.wait(job.id, wait))

        async def get_result(
            job_id: str, thread: Thread = "default", wait: Wait = 0
        ) -> dict[str, Any]:
            owned_job(thread, job_id)
            return result(await core.wait(job_id, wait))

        async def cancel(thread: Thread = "default", job_id: str | None = None) -> dict[str, Any]:
            if job_id is not None:
                owned_job(thread, job_id)
                return {"jobs": [result(await core.cancel_job(job_id))]}
            return {"jobs": [result(job) for job in await core.cancel(conversation(thread))]}

        for suffix, fn, description, readonly in (
            ("status", status, "Agent connection status and running jobs for this thread.", True),
            (
                "sessions",
                sessions,
                "List this thread's sessions and active gateway session id.",
                True,
            ),
            (
                "new_session",
                new_session,
                "Create and activate a new session for this thread.",
                False,
            ),
            (
                "ask",
                ask,
                "Submit text; return a final answer or a running job_id. Optional session_id "
                "targets an owned session without changing the active one.",
                False,
            ),
            (
                "result",
                get_result,
                "Get a job's final answer or running status using its original thread.",
                True,
            ),
            ("cancel", cancel, "Cancel a job or all running jobs of this thread.", False),
        ):
            self.server.add_tool(
                fn,
                name=f"{alias}_{suffix}",
                description=description,
                annotations=ToolAnnotations(readOnlyHint=readonly, openWorldHint=True),
            )
