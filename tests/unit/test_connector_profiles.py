"""Profile changes preserve local authority, history and atomic configuration."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from acp_gateway.config import AppConfig, SecretStore, Settings
from acp_gateway.connectors.launch import ConnectorLaunch, local_policies
from acp_gateway.connectors.maintenance import ConnectorMaintenance
from acp_gateway.connectors.profiles import LocalProfiles, revision
from acp_gateway.connectors.protocol import ProfileSpec, ProfilesRequest


@pytest.fixture
def local(tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    raw = {
        "data_dir": str(tmp_path / "data"),
        "connector": {"remote_profiles": True},
        "agents": [
            {
                "alias": alias,
                "kind": "codex",
                "backend": "codex",
                "default_cwd": str(cwd),
                "codex": {"executable": "/local/trusted/codex", "mode": "safe"},
            }
            for alias in ("first", "second")
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = AppConfig(Settings(**raw), SecretStore({}), path, None)
    launch = ConnectorLaunch("ws://127.0.0.1/connect", "home", tmp_path / "key")
    managers = {}
    maintenance = ConnectorMaintenance(config.settings.agents, managers)
    return LocalProfiles(config, launch, maintenance, {}, local_policies(config), managers)


def request(local, action="list", **kwargs):
    return ProfilesRequest(
        epoch=uuid4(),
        request=uuid4(),
        operation=uuid4(),
        actor="telegram:123",
        expires=time.time() + 90,
        action=action,
        revision=revision(local.config.config_path.read_bytes()),
        **kwargs,
    )


def spec(local, alias="third", **kwargs):
    return ProfileSpec(
        alias=alias,
        template="first",
        title=alias,
        cwd=local.config.settings.agents[0].default_cwd,
        mode="safe",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_add_preserves_executable_env_and_makes_backup(local):
    original = local.config.config_path.read_bytes()
    result = await local.request(request(local, "add", profile=spec(local)))
    assert result.status == "ok"
    assert [p.alias for p in result.profiles] == ["first", "second", "third"]
    added = local.config.settings.agents[-1]
    assert added.codex.executable == "/local/trusted/codex"
    assert not local.maintenance.status()["reserved"]
    assert (
        next(
            (local.config.config_path.parent / "setup-backups" / "profiles").glob("*.yaml")
        ).read_bytes()
        == original
    )
    assert "third" in local.clients and "third" in local.policies


@pytest.mark.asyncio
async def test_disabled_no_discovery_or_write(local):
    local.config.settings.connector.remote_profiles = False
    assert (await local.request(request(local))).status == "disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 121])
async def test_connector_still_refuses_expired_or_excessive_rpc_deadline(local, offset):
    req = request(local).model_copy(update={"expires": time.time() + offset})
    assert (await local.request(req)).status == "stale"


@pytest.mark.asyncio
async def test_retry_identical_operation_does_not_repeat(local):
    req = request(local, "add", profile=spec(local))
    first = await local.request(req)
    second = await local.request(req.model_copy(update={"request": uuid4(), "epoch": uuid4()}))
    assert first == second
    assert len(local.config.settings.agents) == 3
    assert (await local.request(request(local, "add", profile=spec(local)))).status == "invalid"


@pytest.mark.asyncio
async def test_operation_id_cannot_be_reused_with_new_arguments_or_revision(local):
    original = local.config.config_path.read_bytes()
    req = request(local, "add", profile=spec(local))
    assert (await local.request(req)).status == "ok"
    before = local.config.config_path.read_bytes()
    reused = request(local, "remove", alias="second").model_copy(
        update={"operation": req.operation}
    )
    assert (await local.request(reused)).status == "invalid"
    assert local.config.config_path.read_bytes() == before
    backup = (
        local.config.config_path.parent
        / "setup-backups"
        / "profiles"
        / (req.operation.hex + ".yaml")
    )
    assert backup.read_bytes() == original


@pytest.mark.asyncio
async def test_stale_form_and_changed_payload_cannot_overwrite(local):
    old = request(local, "remove", alias="first")
    req = request(local, "add", profile=spec(local))
    assert (await local.request(req)).status == "ok"
    before = local.config.config_path.read_bytes()
    assert (await local.request(old)).status == "stale"
    assert (
        await local.request(
            req.model_copy(update={"action": "remove", "profile": None, "alias": "second"})
        )
    ).status == "invalid"
    assert local.config.config_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["pending_requests", "pending_approvals", "running_sessions", "uncertain_sessions", "opening"],
)
async def test_busy_or_unknown_refuses_without_touching_runtime(local, field):
    manager = SimpleNamespace(
        maintenance_reserved=False, maintenance_status=lambda: {field: 1}, close=AsyncMock()
    )
    local.managers["first"] = manager
    before = local.config.config_path.read_bytes()
    assert (await local.request(request(local, "remove", alias="first"))).status == "busy"
    assert local.config.config_path.read_bytes() == before
    manager.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_cleanup_fences_and_does_not_save(local):
    counts = {}

    async def close():
        counts["uncertain_sessions"] = 1

    local.managers["first"] = SimpleNamespace(
        maintenance_reserved=False, maintenance_status=lambda: counts, close=close
    )
    before = local.config.config_path.read_bytes()
    result = await local.request(request(local, "remove", alias="first"))
    assert result.status == "unknown"
    assert local.config.config_path.read_bytes() == before
    assert local.maintenance.status()["reserved"]


@pytest.mark.asyncio
async def test_remove_keeps_history_and_rejects_last_profile(local):
    history = local.config.settings.resolved_data_dir() / "pins" / "history"
    history.parent.mkdir(parents=True)
    history.write_text("keep")
    assert (await local.request(request(local, "remove", alias="first"))).status == "ok"
    assert history.read_text() == "keep"
    assert (await local.request(request(local, "remove", alias="second"))).status == "invalid"


@pytest.mark.asyncio
async def test_target_directory_must_exist(local):
    value = spec(local).model_copy(update={"cwd": str(local.config.config_path.parent / "missing")})
    assert (await local.request(request(local, "add", profile=value))).status == "invalid"


@pytest.mark.asyncio
async def test_config_failure_does_not_claim_applied(local, monkeypatch):
    import acp_gateway.connectors.profiles as module

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_atomic", fail)
    before = local.config.config_path.read_bytes()
    assert (await local.request(request(local, "add", profile=spec(local)))).status == "invalid"
    assert local.config.config_path.read_bytes() == before
    assert "third" not in local.clients


@pytest.mark.asyncio
async def test_local_edit_of_remote_opt_in_is_honoured(local):
    raw = yaml.safe_load(local.config.config_path.read_bytes())
    raw["connector"]["remote_profiles"] = False
    local.config.config_path.write_text(yaml.safe_dump(raw))
    assert (await local.request(request(local, "add", profile=spec(local)))).status == "disabled"


@pytest.mark.asyncio
async def test_codex_manager_keeps_uncertain_cleanup_when_reconfiguring(local):
    import asyncio

    from acp_gateway.adapters.codex import CodexProcessManager

    class Stream:
        uncertain = 0
        _closing = None

        def __init__(self):
            self._requests, self._permissions, self._turns = {}, {}, {}

        async def close(self):
            self.uncertain = 1
            self._closing = asyncio.get_running_loop().create_future()
            self._closing.set_result(None)

    profile = local.config.settings.agents[0]
    manager = CodexProcessManager(profile.codex, local.policies["first"])
    manager._streams.add(Stream())
    local.managers["first"] = manager
    before = local.config.config_path.read_bytes()
    assert (await local.request(request(local, "remove", alias="first"))).status == "unknown"
    assert local.config.config_path.read_bytes() == before


@pytest.mark.parametrize("initialization_fails", [False, True])
async def test_first_discovered_agent_does_not_need_existing_managed_template(
    local, monkeypatch, initialization_fails
):
    import acp_gateway.connectors.profiles as module
    from acp_gateway.config import AgentProfile

    # Existing profiles are unrelated execution identities, e.g. a Hermes-only PC.
    candidate = AgentProfile.model_validate(
        {
            "alias": "claude_code",
            "kind": "claude_code",
            "backend": "claude_code",
            "default_cwd": local.config.settings.agents[0].default_cwd,
            "claude_code": {"executable": "/local/discovered/claude"},
        }
    )
    local.discovery.candidates["a" * 32] = candidate
    local.discovery.expires = time.monotonic() + 300
    connect, close = AsyncMock(), AsyncMock()
    if initialization_fails:
        connect.side_effect = ValueError("private details")
    monkeypatch.setattr(
        module, "AgentClient", lambda *a, **kw: SimpleNamespace(connect=connect, close=close)
    )
    value = ProfileSpec(
        alias="claude", candidate="a" * 32, title="Claude", cwd=candidate.default_cwd, mode="work"
    )
    req = request(local, "add", profile=value)
    result = await local.request(req)
    assert result.status == "ok"
    assert result.initialization == ("failed" if initialization_fails else "ok")
    saved = local.config.settings.agents[-1]
    assert saved.backend == "claude_code"
    assert saved.claude_code.executable == "/local/discovered/claude"
    assert saved.claude_code.mode == "work"
    connect.assert_awaited_once()
    close.assert_awaited_once()
    assert (await local.request(req)).status == "ok"
    assert connect.await_count == 1
    assert not local.maintenance.status()["reserved"]


async def test_forged_expired_and_update_candidate_ids_do_not_change_config(local):
    local.discovery.candidates["a" * 32] = local.config.settings.agents[0]
    local.discovery.expires = time.monotonic() + 30
    original = local.config.config_path.read_bytes()
    value = ProfileSpec(
        alias="third",
        candidate="b" * 32,
        title="Code",
        cwd=local.config.settings.agents[0].default_cwd,
        mode="safe",
    )
    assert (await local.request(request(local, "add", profile=value))).status == "invalid"
    value = value.model_copy(update={"candidate": "a" * 32, "alias": "first"})
    assert (await local.request(request(local, "update", profile=value))).status == "invalid"
    local.discovery.expires = time.monotonic() - 1
    value = value.model_copy(update={"alias": "third"})
    assert (await local.request(request(local, "add", profile=value))).status == "invalid"
    assert local.config.config_path.read_bytes() == original


async def test_discovery_does_not_reserve_or_change_profiles(local):
    original = local.config.config_path.read_bytes()
    local.discovery.discover = AsyncMock(return_value=([], ["Install locally"]))
    result = await local.request(request(local, "discover"))
    assert result.status == "ok" and result.candidates == [] and result.hints
    assert local.config.config_path.read_bytes() == original
    assert not local.maintenance.status()["reserved"]
    assert not local.clients
