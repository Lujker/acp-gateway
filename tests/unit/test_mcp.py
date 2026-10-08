"""MCP alias boundaries and sanitized tool errors without network access."""

import pytest
from pydantic import SecretStr

from acp_gateway.agents import AgentClient
from acp_gateway.channels.mcp import McpChannel
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
from acp_gateway.core import GatewayCore
from acp_gateway.daemon import configured_app
from acp_gateway.storage import Conversation, JobStatus, Store


@pytest.fixture
async def channel(tmp_path):
    agents = {
        alias: AgentClient(
            AgentProfile(alias=alias, url="ws://localhost/acp", default_cwd="/work"),
            pin_dir=tmp_path / "pins",
        )
        for alias in ("work", "other")
    }
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), agents)
    channel = McpChannel(core, SecretStr("mcp-test-credential"))
    try:
        yield channel, core
    finally:
        await core.close()


async def test_alias_cannot_read_or_cancel_another_agents_job(channel):
    mcp, core = channel
    record = core.store.add_session(Conversation("mcp", "default", "other"), "s1", "/work")
    job = core.store.add_job("owned-other", record.id)
    core.store.finish_job(job.id, JobStatus.COMPLETED, answer="private-answer")
    for suffix in ("result", "cancel"):
        result = await mcp.server.call_tool("work_" + suffix, {"job_id": job.id})
        assert result.isError and "private-answer" not in str(result)
    sessions = await mcp.server.call_tool("work_sessions", {})
    assert "s1" not in str(sessions)


async def test_unexpected_errors_and_validation_do_not_echo_private_input(channel, monkeypatch):
    mcp, core = channel

    def broken(*args):
        raise RuntimeError("private database details")

    monkeypatch.setattr(core, "sessions", broken)
    result = await mcp.server.call_tool("work_sessions", {})
    assert result.isError and "private database details" not in str(result)
    for value in (301, float("inf"), float("nan")):
        result = await mcp.server.call_tool("work_ask", {"text": "private-input", "wait": value})
        assert result.isError and "private-input" not in str(result)


def test_daemon_rejects_mcp_token_equal_to_agent_secret(tmp_path):
    profile = AgentProfile(
        alias="work", url="ws://localhost/acp", default_cwd="/work", secret_env="AGENT_WORK_SECRET"
    )
    cfg = AppConfig(
        Settings(agents=[profile], data_dir=tmp_path),
        SecretStore(
            {"ACPGW_API_TOKEN": "owner", "ACPGW_MCP_TOKEN": "shared", "AGENT_WORK_SECRET": "shared"}
        ),
        None,
        None,
    )
    with (
        pytest.raises(ValueError, match="MCP and agent credentials must be different"),
        configured_app(cfg),
    ):
        pytest.fail("accepted a shared credential")


def test_daemon_without_mcp_token_has_no_mcp_channel(tmp_path):
    cfg = AppConfig(
        Settings(data_dir=tmp_path), SecretStore({"ACPGW_API_TOKEN": "owner"}), None, None
    )
    with configured_app(cfg) as app:
        assert set(app.state.core.channels) == {"cli"}
