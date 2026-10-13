"""Real core/ACP work through owner HTTP and MCP shares bounded channel budgets."""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from acp_gateway.agents import AgentClient
from acp_gateway.api.app import create_app
from acp_gateway.channels.cli import CliChannel
from acp_gateway.channels.mcp import McpChannel
from acp_gateway.config import AgentProfile, RateLimitSettings
from acp_gateway.core import GatewayCore, RateLimited
from acp_gateway.storage import Conversation, Store
from fakes.fake_goose import FakeGooseServer


@pytest.fixture
async def limited(tmp_path):
    with FakeGooseServer("mock-limiter-secret") as server:
        agent = AgentClient(
            AgentProfile(alias="work", kind="goose", url=server.url("ws"), default_cwd="/work"),
            SecretStr("mock-limiter-secret"),
            pin_dir=tmp_path / "pins",
        )
        core = GatewayCore(
            Store.open(tmp_path / "gateway.db"),
            {"work": agent},
            rate_limits=RateLimitSettings(work_requests=1),
        )
        cli = CliChannel()
        core.add_channel(cli)
        app = create_app(core, cli, SecretStr("owner-limiter-credential"))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer owner-limiter-credential"},
            ) as client,
        ):
            yield core, client


async def test_http_429_precedes_session_side_effects_and_auth_does_not_charge(limited):
    core, client = limited
    body = {"text": "say pong", "thread": "first", "wait": 5}
    rejected = await client.post("/messages", json=body, headers={"Authorization": "Bearer wrong"})
    assert rejected.status_code == 401
    response = await client.post("/messages", json=body)
    assert response.status_code == 202 and response.json()["answer"] == "pong"
    # Implicit session creation consumed no second work slot.
    job_id = response.json()["id"]
    for path, body in [
        ("/messages", {"text": "private-prompt", "thread": "another"}),
        ("/sessions", {"thread": "third"}),
    ]:
        denied = await client.post(path, json=body)
        assert denied.status_code == 429
        assert 1 <= int(denied.headers["retry-after"]) <= 60
        assert denied.headers["cache-control"] == "no-store"
        assert "private-prompt" not in denied.text
    assert core.store.sessions(Conversation("cli", "another", "work")) == []
    assert core.store.sessions(Conversation("cli", "third", "work")) == []
    assert (await client.get("/jobs/" + job_id)).status_code == 200
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/approvals")).status_code == 200
    assert (await client.post("/stop", json={"thread": "first"})).status_code == 200


async def test_mcp_error_and_independent_cli_budget(limited):
    core, client = limited
    mcp = McpChannel(core, SecretStr("mcp-limiter-credential"))
    created = await mcp.server.call_tool("work_new_session", {"thread": "one"})
    assert not getattr(created, "isError", False)
    for name, args in [
        ("work_ask", {"text": "private-prompt", "thread": "two"}),
        ("work_new_session", {"thread": "three"}),
    ]:
        denied = await mcp.server.call_tool(name, args)
        assert denied.isError and "retry after" in denied.content[0].text
        assert "private-prompt" not in str(denied)
    assert core.store.sessions(Conversation("mcp", "two", "work")) == []
    assert core.store.sessions(Conversation("mcp", "three", "work")) == []
    for name in ("work_status", "work_sessions", "work_cancel"):
        result = await mcp.server.call_tool(name, {"thread": "one"})
        assert not getattr(result, "isError", False)
    assert (await client.post("/sessions", json={"thread": "cli"})).status_code == 201


async def test_concurrent_threads_cannot_all_take_last_slot(limited):
    core, _ = limited
    conversations = [Conversation("mcp", str(index), "work") for index in range(10)]
    results = await asyncio.gather(
        *(core.new_session(conv) for conv in conversations), return_exceptions=True
    )
    assert sum(isinstance(result, RateLimited) for result in results) == 9
    assert sum(len(core.store.sessions(conv)) for conv in conversations) == 1
