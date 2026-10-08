"""Owner API. Agent/MCP credentials are never accepted as owner credentials."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import SecretStr

from acp_gateway import __version__
from acp_gateway.agents import AgentError, AuthenticationFailed, SessionBusy
from acp_gateway.channels.cli import CliChannel
from acp_gateway.core import (
    ApprovalNotFound,
    GatewayCore,
    GatewayError,
    JobFinished,
    JobNotFound,
    JobProgress,
    PolicyDenied,
    UnknownAgent,
    UnknownSession,
    for_approver,
)
from acp_gateway.core.bus import Subscription
from acp_gateway.log import get_logger, redact_value
from acp_gateway.storage import Conversation, JobStatus

from .schemas import ApprovalDecision, ConversationRequest, NewSessionRequest, PromptRequest
from .security import OwnerAccess

Wait = Annotated[float, Query(ge=0, le=300, allow_inf_nan=False)]


def _sse(kind: str, data) -> str:
    # Escape Unicode line separators too: common SSE readers split them as lines.
    encoded = json.dumps(jsonable_encoder(data), ensure_ascii=True, separators=(",", ":"))
    return f"event: {kind}\ndata: {encoded}\n\n"


def create_app(core: GatewayCore, cli: CliChannel, token: SecretStr, *, mcp=None) -> FastAPI:
    if not token.get_secret_value():
        raise ValueError("the owner API token is required")

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with AsyncExitStack() as stack:
                if mcp is not None:
                    await stack.enter_async_context(mcp.server.session_manager.run())
                await core.start()
                yield
        finally:
            await core.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.core = core
    app.state.cli = cli
    app.add_middleware(
        OwnerAccess,
        token=token.get_secret_value(),
        max_body_bytes=4 * core.policy.settings.max_prompt_length + 8192,
        mcp_app=mcp.app if mcp is not None else None,
    )

    @app.exception_handler(AgentError)
    @app.exception_handler(GatewayError)
    async def known_error(request, exc):
        if isinstance(exc, (UnknownAgent, UnknownSession, JobNotFound, ApprovalNotFound)):
            status = 404
        elif isinstance(exc, SessionBusy):
            status = 409
        elif isinstance(exc, (PolicyDenied, AuthenticationFailed)):
            status = 403
        elif isinstance(exc, AgentError):
            status = 503
        else:
            status = 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        get_logger(__name__).error("owner API failed", exc_info=exc)
        return JSONResponse(
            {"detail": "internal gateway error"},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Don't echo prompts, credentials or non-finite JSON values in errors.
        errors = [{k: e[k] for k in ("loc", "msg", "type")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    def conversation(agent: str | None, thread: str) -> Conversation:
        if agent is None:
            if len(core.agents) != 1:
                raise HTTPException(400, "select an agent with --agent or the agent field")
            agent = next(iter(core.agents))
        core.agent(agent)
        try:
            return Conversation("cli", thread, agent)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def owned_session(session_id: int, agent: str | None, thread: str):
        conv = conversation(agent, thread)
        session = core.store.session(session_id)
        if session is None or session.conversation != conv:
            raise UnknownSession("this conversation does not own that session")
        return conv, session

    @app.get("/health")
    async def health():
        return {
            "gateway": {"version": __version__, "status": "running"},
            "database": {"schema_version": core.store.schema_version},
            "agents": [
                {
                    "alias": a,
                    "title": c.profile.display_name,
                    "connected": c.connected,
                    "info": c.agent_info,
                }
                for a, c in core.agents.items()
            ],
            "channels": [
                {"name": c.name, "connected": c.connected, "can_approve": c.can_approve}
                for c in core.channels.values()
            ],
            "running_jobs": len(core.running_jobs()),
        }

    @app.get("/sessions")
    async def sessions(agent: str | None = None, thread: str = "default"):
        conv = conversation(agent, thread)
        active = core.active_session(conv)
        return {
            "sessions": jsonable_encoder(core.sessions(conv)),
            "active_session_id": active.id if active else None,
        }

    @app.post("/sessions", status_code=201)
    async def new_session(body: NewSessionRequest):
        return jsonable_encoder(
            await core.new_session(conversation(body.agent, body.thread), body.cwd)
        )

    @app.post("/sessions/{session_id}/activate")
    async def activate(session_id: int, body: ConversationRequest):
        conv, session = owned_session(session_id, body.agent, body.thread)
        return jsonable_encoder(core.switch_session(conv, session.acp_session_id))

    async def prompt(body: PromptRequest, session_id: int | None = None):
        conv = conversation(body.agent, body.thread)
        acp_session_id = None
        if session_id is not None:
            conv, session = owned_session(session_id, body.agent, body.thread)
            acp_session_id = session.acp_session_id
        job = await core.submit(conv, body.text, acp_session_id=acp_session_id)
        return jsonable_encoder(await core.wait(job.id, body.wait))

    @app.post("/messages", status_code=202)
    async def messages(body: PromptRequest):
        return await prompt(body)

    @app.post("/sessions/{session_id}/messages", status_code=202)
    async def session_messages(session_id: int, body: PromptRequest):
        return await prompt(body, session_id)

    @app.get("/jobs/{job_id}")
    async def job(job_id: str, wait: Wait = 0):
        return jsonable_encoder(await core.wait(job_id, wait))

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        return jsonable_encoder(await core.cancel_job(job_id))

    @app.post("/stop")
    async def stop(body: ConversationRequest):
        return jsonable_encoder(await core.cancel(conversation(body.agent, body.thread)))

    async def stream(events: Subscription, *, snapshot=None, job_id=None):
        try:
            if snapshot is not None:
                yield _sse("snapshot", snapshot)
                if snapshot.status is not JobStatus.RUNNING:
                    return
            dropped = 0
            while True:
                if events.dropped != dropped:
                    dropped = events.dropped
                    # These chunks precede the snapshot and must not be replayed after it.
                    events.discard_pending()
                    current = core.job(job_id) if job_id else {"dropped": dropped}
                    yield _sse("resync", current)
                    if job_id and current.status.finished:
                        return
                try:
                    event = await asyncio.wait_for(events.get(), 10)
                except TimeoutError:
                    if job_id:
                        current = core.job(job_id)
                        yield _sse("resync", current)
                        if current.status.finished:
                            return
                    else:
                        yield ": keepalive\n\n"
                    continue
                if event is None:
                    return
                data = jsonable_encoder(event)
                if isinstance(event, JobProgress):
                    data["event_type"] = type(event.event).__name__
                yield _sse(type(event).__name__, redact_value(data))
                if isinstance(event, JobFinished) and job_id:
                    return
        finally:
            events.close()

    def sse_response(iterator):
        return StreamingResponse(
            iterator, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str):
        core.job(job_id)

        async def watch_job():
            with core.bus.subscribe(
                lambda e: (
                    isinstance(e, JobProgress | JobFinished)
                    and (e.job_id if isinstance(e, JobProgress) else e.job.id) == job_id
                )
            ) as events:
                snapshot = core.job(job_id)
                async for frame in stream(events, snapshot=snapshot, job_id=job_id):
                    yield frame

        return sse_response(watch_job())

    @app.get("/events")
    async def all_events():
        async def watch_all():
            with core.bus.subscribe() as events:
                async for frame in stream(events):
                    yield frame

        return sse_response(watch_all())

    @app.get("/approvals")
    async def approvals():
        pending = core.pending_approvals("cli") if core.policy.can_approve(cli) else []
        return {"approvals": jsonable_encoder(pending)}

    @app.get("/approvals/events")
    async def approval_events():
        async def watch():
            lease = cli.attach("local-api-owner")
            try:
                events = core.bus.subscribe(for_approver("cli"))
                with events:
                    pending = core.pending_approvals("cli") if core.policy.can_approve(cli) else []
                    yield _sse("connected", {"lease_id": lease, "approvals": pending})
                    async for frame in stream(events):
                        yield frame
            finally:
                cli.detach(lease)

        return sse_response(watch())

    @app.post("/approvals/{approval_id}")
    async def decide(approval_id: str, body: ApprovalDecision):
        actor = cli.actor(body.lease_id)
        core.resolve_approval(approval_id, body.option_id, channel="cli", actor=actor)
        return {"status": "settled"}

    return app
