"""Owner authentication and request bounds, without opening network sockets."""

import httpx
import pytest
from pydantic import SecretStr

from acp_gateway.agents.events import MessageChunk
from acp_gateway.api.app import create_app
from acp_gateway.channels.cli import CliChannel
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
from acp_gateway.core import GatewayCore, JobFinished, JobProgress, PolicyDenied
from acp_gateway.daemon import configured_app, owner_token
from acp_gateway.storage import Conversation, JobStatus, Store

OWNER = "test-owner-credential"


@pytest.fixture
async def api(tmp_path):
    core = GatewayCore(Store.open(tmp_path / "gateway.db"), {})
    cli = CliChannel()
    core.add_channel(cli)
    app = create_app(core, cli, SecretStr(OWNER))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {OWNER}"},
        ) as client,
    ):
        yield client, core, cli


@pytest.mark.parametrize("auth", ["", "Bearer wrong", "Bearer mcp-credential", OWNER])
async def test_owner_authentication_precedes_routes(api, auth):
    client, _, _ = api
    response = await client.get("/health", headers={"Authorization": auth})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert OWNER not in response.text


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "127.0.0.1.evil.example",
        "localhost@evil.example",
        "127.0.0.1/path",
        "localhost:99999",
        "[::1",
        "",
    ],
)
async def test_untrusted_host_is_rejected(api, host):
    client, _, _ = api
    assert (await client.get("/health", headers={"Host": host})).status_code == 400


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1", "[::1]:8765"])
async def test_loopback_health_without_human_lease(api, host):
    client, core, cli = api
    response = await client.get("/health", headers={"Host": host})
    assert response.status_code == 200
    assert response.json()["database"]["schema_version"] == core.store.schema_version
    assert response.json()["channels"] == [{"name": "cli", "connected": False, "can_approve": True}]
    assert not cli.connected
    assert (await client.get("/approvals")).json() == {"approvals": []}
    assert not cli.connected


async def test_request_bounds_and_errors_do_not_echo_input(api):
    client, _, _ = api
    assert (await client.post("/messages", content=b"x" * 210_000)).status_code == 413
    response = await client.post("/messages", json={"text": OWNER, "unexpected": OWNER})
    assert response.status_code == 422
    assert OWNER not in response.text
    response = await client.post(
        "/messages",
        content='{"text":"secret-input", "wait": NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "secret-input" not in response.text
    assert (await client.get("/jobs/unknown?wait=inf")).status_code == 422
    assert (await client.get("/jobs/unknown")).status_code == 404
    assert (await client.get("/jobs/unknown/events")).status_code == 404
    assert (await client.get("/sessions?agent=missing")).status_code == 404
    assert (await client.get("/openapi.json")).status_code == 404


async def test_cli_lease_is_live_only_until_detach(api):
    _, _, cli = api
    lease = cli.attach("owner")
    assert cli.connected and cli.actor(lease) == "owner"
    second = cli.attach("another-owner")
    cli.detach(lease)
    assert cli.connected
    with pytest.raises(PolicyDenied):
        cli.actor(lease)
    await cli.stop()
    assert not cli.connected
    with pytest.raises(PolicyDenied):
        cli.actor(second)
    with pytest.raises(PolicyDenied):
        cli.attach("owner")


async def test_component_health_reports_unavailable_and_authenticates_probes(api):
    client, core, cli = api
    assert (await client.get("/health/unknown")).status_code == 404
    response = await client.get("/health/cli")
    assert response.status_code == 503 and response.json()["connected"] is False
    lease = cli.attach("owner")
    assert (await client.get("/health/channels/cli")).status_code == 200
    cli.detach(lease)

    class Agent:
        connected = False
        agent_info = None
        calls = 0

        async def connect(self):
            self.calls += 1
            self.connected = True

        async def close(self):
            pass

    agent = Agent()
    core._agents["work"] = agent
    assert (await client.get("/health/work")).status_code == 503
    assert agent.calls == 0
    assert (
        await client.get("/health/work?check=true", headers={"Authorization": ""})
    ).status_code == 401
    assert agent.calls == 0
    response = await client.get("/health/agents/work?check=true")
    assert response.status_code == 200 and response.json()["connected"] is True
    assert agent.calls == 1


def config(tmp_path, secrets):
    return AppConfig(Settings(data_dir=tmp_path), SecretStore(secrets), None, None)


def test_daemon_requires_separate_owner_credential(tmp_path):
    with pytest.raises(ValueError, match="missing owner API token"):
        owner_token(config(tmp_path, {}))
    with pytest.raises(ValueError, match="must be different"):
        owner_token(config(tmp_path, {"ACPGW_API_TOKEN": OWNER, "ACPGW_MCP_TOKEN": OWNER}))
    profile = AgentProfile(
        alias="work", url="ws://localhost/acp", default_cwd="/work", secret_env="AGENT_WORK_SECRET"
    )
    cfg = AppConfig(
        Settings(agents=[profile]),
        SecretStore({"ACPGW_API_TOKEN": OWNER, "AGENT_WORK_SECRET": OWNER}),
        None,
        None,
    )
    with pytest.raises(ValueError, match="agent credentials must be different"):
        owner_token(cfg)


def test_second_daemon_is_blocked_and_lock_released(tmp_path):
    cfg = config(tmp_path, {"ACPGW_API_TOKEN": OWNER})
    with configured_app(cfg) as app:
        assert app.state.core.store.schema_version == 3
        with pytest.raises(ValueError, match="another gateway daemon"), configured_app(cfg):
            pytest.fail("second daemon acquired the lock")
    with configured_app(cfg):
        pass


async def test_sse_overflow_discards_chunks_before_resync(api):
    _, core, cli = api
    conv = Conversation("cli", "default", "work")
    session = core.store.add_session(conv, "acp1", "/work")
    job = core.store.add_job("stream1", session.id)
    app = create_app(core, cli, SecretStr(OWNER))
    endpoint = next(r.endpoint for r in app.routes if r.path == "/jobs/{job_id}/events")
    response = await endpoint(job.id)
    iterator = response.body_iterator
    assert "event: snapshot" in await anext(iterator)
    for _ in range(1001):
        core.bus.publish(JobProgress(conv, job.id, MessageChunk("acp1", "stale")))
    assert "event: resync" in await anext(iterator)
    core.bus.publish(JobProgress(conv, job.id, MessageChunk("acp1", "fresh")))
    finished = core.store.finish_job(job.id, JobStatus.COMPLETED, answer="fresh")
    core.bus.publish(JobFinished(conv, finished))
    frame = await anext(iterator)
    assert "fresh" in frame and "stale" not in frame
    assert "event: JobFinished" in await anext(iterator)
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert core.bus.subscriber_count == 0
