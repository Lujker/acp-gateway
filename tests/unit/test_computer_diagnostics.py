import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from acp_gateway.connectors.computer_diagnostics import ComputerDiagnostics, activity_reason
from acp_gateway.connectors.maintenance import ConnectorMaintenance
from acp_gateway.connectors.protocol import RuntimeRequest


@pytest.mark.parametrize(
    "status,expected",
    [
        ({}, "idle"),
        ({"running_sessions": 2}, "busy"),
        ({"uncertain_sessions": 1}, "uncertain"),
        ({"reserved": True}, "reserved"),
        ({"unsupported_agents": 1}, "unsupported"),
        (None, "unknown"),
    ],
)
def test_activity_reason_is_bounded_to_fixed_codes(status, expected):
    assert activity_reason(status) == expected


async def test_probe_refuses_during_maintenance_without_opening_agent(tmp_path):
    profile = SimpleNamespace(alias="code", backend="codex")
    maintenance = ConnectorMaintenance([profile], {})
    maintenance.token = "reserved"
    config = SimpleNamespace(
        settings=SimpleNamespace(
            agents=[profile],
            updates=SimpleNamespace(enabled=False),
            resolved_data_dir=lambda: tmp_path,
        )
    )
    report = SimpleNamespace(
        can_update=False,
        platform="linux",
        manager="uv",
        source="registry",
        installed_version="0.2.8",
    )
    factory = AsyncMock()
    service = ComputerDiagnostics(
        config, maintenance, SimpleNamespace(report=lambda: report), {}, factory
    )
    maintenance.services.append(service)
    result = await service.request(
        RuntimeRequest(epoch=uuid4(), request=uuid4(), action="initialize", alias="code")
    )
    assert result.diagnostics.probe == "busy"
    assert result.diagnostics.maintenance == "reserved"
    assert result.diagnostics.updater == "disabled"
    factory.assert_not_awaited()


async def test_cancelled_probe_keeps_maintenance_fence_until_cleanup(tmp_path, monkeypatch):
    profile = SimpleNamespace(alias="code", backend="codex")
    maintenance = ConnectorMaintenance([profile], {})
    config = SimpleNamespace(
        settings=SimpleNamespace(agents=[profile], resolved_data_dir=lambda: tmp_path)
    )
    cleanup_started, release = asyncio.Event(), asyncio.Event()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            pass

        async def close(self):
            cleanup_started.set()
            await release.wait()

    monkeypatch.setattr("acp_gateway.connectors.computer_diagnostics.AgentClient", FakeClient)
    service = ComputerDiagnostics(config, maintenance, None, {}, AsyncMock())
    maintenance.services.append(service)
    task = asyncio.create_task(service._probe("code"))
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert maintenance.status()["opening"] == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert maintenance.status()["opening"] == 0


async def test_direct_probe_does_not_change_host_tofu_pin(tmp_path, monkeypatch):
    profile = SimpleNamespace(alias="remote", backend="remote", secret_env=None)
    maintenance = ConnectorMaintenance([profile], {})
    config = SimpleNamespace(
        settings=SimpleNamespace(agents=[profile], resolved_data_dir=lambda: tmp_path), secrets=None
    )
    seen = []

    class FakeClient:
        def __init__(self, *args, pin_dir, **kwargs):
            self.pin = pin_dir / "remote.sha256"
            seen.append(self.pin)

        async def connect(self):
            self.pin.write_text("temporary fingerprint")

        async def close(self):
            pass

    monkeypatch.setattr("acp_gateway.connectors.computer_diagnostics.AgentClient", FakeClient)
    service = ComputerDiagnostics(config, maintenance, None, {}, AsyncMock())
    assert await service._probe("remote") == "ok"
    assert seen[0].parent != tmp_path / "pins"
    assert not (tmp_path / "pins" / "remote.sha256").exists()
    assert not seen[0].exists()


async def test_failed_claude_open_with_unconfirmed_cleanup_keeps_maintenance_fenced(
    tmp_path, monkeypatch
):
    from acp_gateway.adapters.claude_code import ClaudeCodeAcpTransport, ClaudeCodeProcessManager
    from acp_gateway.agents.errors import AgentUnavailable
    from acp_gateway.config import AgentProfile
    from acp_gateway.connectors.policy import LocalAgentPolicy

    async def fail_spawn(self, *args, **kwargs):
        raise AgentUnavailable("initialization failed")

    async def unconfirmed(self):
        self.cleanup_unconfirmed = True
        self.uncertain = 1

    monkeypatch.setattr(ClaudeCodeAcpTransport, "_spawn", fail_spawn)
    monkeypatch.setattr(ClaudeCodeAcpTransport, "_shutdown_children", unconfirmed)
    profile = AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd=str(tmp_path)
    )
    manager = ClaudeCodeProcessManager(profile.claude_code, LocalAgentPolicy((str(tmp_path),)))
    maintenance = ConnectorMaintenance([profile], {"claude": manager})
    config = SimpleNamespace(
        settings=SimpleNamespace(agents=[profile], resolved_data_dir=lambda: tmp_path)
    )
    service = ComputerDiagnostics(
        config,
        maintenance,
        None,
        {},
        lambda alias, diagnostic_failure=None: manager.open(on_failed_cleanup=diagnostic_failure),
    )
    maintenance.services.append(service)
    assert await service._probe("claude") == "cleanup_unknown"
    assert maintenance.status()["uncertain_sessions"] == 1
    assert not maintenance.status()["idle_observed"]
    with pytest.raises(ValueError, match="busy"):
        maintenance.apply("prepare")


async def test_claude_failed_open_keeps_fence_through_repeated_cancellation(tmp_path, monkeypatch):
    from acp_gateway.adapters.claude_code import ClaudeCodeAcpTransport, ClaudeCodeProcessManager
    from acp_gateway.agents.errors import AgentUnavailable
    from acp_gateway.config import AgentProfile
    from acp_gateway.connectors.policy import LocalAgentPolicy

    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_spawn(self, *args, **kwargs):
        raise AgentUnavailable("initialize failed")

    async def shutdown(self):
        entered.set()
        await release.wait()

    monkeypatch.setattr(ClaudeCodeAcpTransport, "_spawn", fail_spawn)
    monkeypatch.setattr(ClaudeCodeAcpTransport, "_shutdown_children", shutdown)
    profile = AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd=str(tmp_path)
    )
    manager = ClaudeCodeProcessManager(profile.claude_code, LocalAgentPolicy((str(tmp_path),)))
    task = asyncio.create_task(manager.open())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert manager.maintenance_status()["opening"] == 1
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.maintenance_status()["opening"] == 0


async def test_claude_capacity_is_reported_as_busy(tmp_path):
    from acp_gateway.agents.errors import ManagedCapacity
    from acp_gateway.config import AgentProfile

    profile = AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd=str(tmp_path)
    )
    maintenance = ConnectorMaintenance([profile], {})
    config = SimpleNamespace(
        settings=SimpleNamespace(agents=[profile], resolved_data_dir=lambda: tmp_path)
    )

    async def factory(alias, **kwargs):
        raise ManagedCapacity("capacity")

    service = ComputerDiagnostics(config, maintenance, None, {}, factory)
    maintenance.services.append(service)
    assert await service._probe("claude") == "busy"


async def test_probe_does_not_attribute_another_dialogues_uncertainty(tmp_path, monkeypatch):
    from acp_gateway.config import AgentProfile

    profile = AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd=str(tmp_path)
    )
    other = {"uncertain_sessions": 0}
    manager = SimpleNamespace(maintenance_status=lambda: other)
    maintenance = ConnectorMaintenance([profile], {"claude": manager})
    config = SimpleNamespace(
        settings=SimpleNamespace(agents=[profile], resolved_data_dir=lambda: tmp_path)
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            other["uncertain_sessions"] = 1

        async def close(self):
            pass

    monkeypatch.setattr("acp_gateway.connectors.computer_diagnostics.AgentClient", FakeClient)
    service = ComputerDiagnostics(config, maintenance, None, {}, AsyncMock())
    maintenance.services.append(service)
    assert await service._probe("claude") == "ok"
    assert maintenance.status()["uncertain_sessions"] == 1


async def test_slow_updater_report_is_cached_without_short_timeout(tmp_path):
    import time

    from acp_gateway.connectors.protocol import RuntimeRequest

    maintenance = ConnectorMaintenance([], {})
    config = SimpleNamespace(
        settings=SimpleNamespace(agents=[], updates=SimpleNamespace(enabled=True))
    )
    calls = []

    def slow_report():
        calls.append(1)
        time.sleep(5.2)
        return SimpleNamespace(can_update=True, installed_version="0.2.8")

    service = ComputerDiagnostics(
        config, maintenance, SimpleNamespace(report=slow_report), {}, None
    )
    request = RuntimeRequest(epoch=uuid4(), request=uuid4(), action="diagnostics")
    assert (await service.request(request)).diagnostics.updater == "ready"
    assert (await service.request(request)).diagnostics.updater == "ready"
    assert len(calls) == 1
