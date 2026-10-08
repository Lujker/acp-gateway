"""Actual MCP SDK over HTTP, recorded ACP traffic and owner approval API."""

from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_daemon_cli import MCP, OWNER, SECRET, cli
from test_daemon_cli import running as running

from acp_gateway.cli.client import events


@asynccontextmanager
async def connection(running):
    owner, _, _, _ = running
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {MCP}"}, trust_env=False, timeout=10
        ) as http,
        streamable_http_client(str(owner.base_url).rstrip("/") + "/mcp", http_client=http) as (
            read,
            write,
            _,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def call(session, name, **arguments):
    result = await session.call_tool("work_" + name, arguments)
    assert not result.isError, result.content
    assert not any(secret in str(result) for secret in (MCP, OWNER, SECRET))
    return result.structuredContent


async def test_tools_multiturn_threads_and_owned_sessions(running):
    async with connection(running) as session:
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == {
            "work_" + s for s in ("status", "sessions", "new_session", "ask", "result", "cancel")
        }
        assert not running[0].get("/health").json()["channels"][0]["connected"]
        await call(session, "ask", text="remember code word peach", thread="research")
        assert (await call(session, "ask", text="What was the code word", thread="research"))[
            "answer"
        ] == "peach"
        assert (await call(session, "ask", text="What was the code word", thread="other"))[
            "answer"
        ] == "unknown"
        old = (await call(session, "sessions", thread="research"))["active_session_id"]
        new = await call(session, "new_session", thread="research")
        assert new["id"] != old
        assert (
            await call(
                session, "ask", text="What was the code word", thread="research", session_id=old
            )
        )["answer"] == "peach"
        assert (await call(session, "sessions", thread="research"))["active_session_id"] == new[
            "id"
        ]
        forbidden = await session.call_tool(
            "work_ask", {"text": "private", "thread": "other", "session_id": old}
        )
        assert forbidden.isError and "does not own" in str(forbidden.content)
        assert cli(running, "ask", "What was the code word").stdout.strip() == "unknown"


async def test_jobs_poll_after_disconnect_and_isolate_owner_and_thread(running):
    owner = running[0]
    owner_job = owner.post("/messages", json={"text": "pong", "wait": 5}).json()
    async with connection(running) as session:
        job = await call(session, "ask", text="pong", wait=0, thread="a")
        assert job["status"] == "running" and job["answer"] is None
        for name, job_id, thread in (
            ("result", job["job_id"], "b"),
            ("cancel", job["job_id"], "b"),
            ("result", owner_job["id"], "default"),
            ("cancel", owner_job["id"], "default"),
        ):
            result = await session.call_tool("work_" + name, {"job_id": job_id, "thread": thread})
            assert result.isError
    async with connection(running) as session:
        final = await call(session, "result", job_id=job["job_id"], thread="a", wait=5)
        assert final["answer"] == "pong" and final["status"] == "completed"


@pytest.mark.parametrize("action,answer", [("approve", "hi"), ("reject", "DENIED")])
async def test_mcp_action_decided_by_human_cli(running, action, answer):
    owner = running[0]
    async with connection(running) as session:
        with owner.stream("GET", "/approvals/events") as response:
            frames = events(response)
            next(frames)
            job = await call(session, "ask", text="run exactly: echo hi .", wait=0)
            kind, data = next(frames)
            assert kind == "ApprovalRequested"
            assert data["approval"]["conversation"]["channel"] == "mcp"
            cli(running, "approvals", action, data["approval"]["id"])
            final = await call(session, "result", job_id=job["job_id"], wait=5)
            assert final["answer"] == answer


async def test_mcp_no_human_denied_and_cancel_pending_permission(running):
    owner = running[0]
    async with connection(running) as session:
        denied = await call(session, "ask", text="run exactly: echo hi .", wait=5)
        assert denied["answer"] == "DENIED"
        assert denied["error"] == "no human approver is connected"
        with owner.stream("GET", "/approvals/events") as response:
            frames = events(response)
            next(frames)
            job = await call(session, "ask", text="run exactly: sleep 30 .", wait=0)
            assert next(frames)[0] == "ApprovalRequested"
            busy = await session.call_tool("work_ask", {"text": "pong", "wait": 0})
            assert busy.isError and "still working" in str(busy.content)
            cancelled = await call(session, "cancel", job_id=job["job_id"])
            assert cancelled["jobs"][0]["status"] == "cancelled"
            assert owner.get("/approvals").json() == {"approvals": []}


async def test_mcp_authentication_host_bounds_and_safe_validation(running):
    owner = running[0]
    for token in (OWNER, SECRET, "wrong", ""):
        auth = f"Bearer {token}" if token else ""
        assert owner.post("/mcp", headers={"Authorization": auth}).status_code == 401
    assert owner.get("/health", headers={"Authorization": f"Bearer {MCP}"}).status_code == 401
    assert (
        owner.post("/approvals/fake", headers={"Authorization": f"Bearer {MCP}"}).status_code == 401
    )
    headers = {"Authorization": f"Bearer {MCP}"}
    assert owner.post("/mcp", headers={**headers, "Host": "evil.example"}).status_code == 400
    assert owner.post("/mcp", headers=headers, content=b"x" * 700_000).status_code == 413
    async with connection(running) as session:
        for args in (
            {"text": "secret-input", "wait": -1},
            {"text": "secret-input", "thread": ""},
            {"text": "secret-input", "extra": "secret-input"},
            {"text": {"secret-input": 1}},
        ):
            result = await session.call_tool("work_ask", args)
            assert result.isError and "secret-input" not in str(result.content)
        forbidden = await session.call_tool("work_approve", {"option_id": "allow_once"})
        assert forbidden.isError
