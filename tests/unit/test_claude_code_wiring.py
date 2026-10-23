"""Managed Claude Code client and connector safety wiring."""

from types import SimpleNamespace

import pytest

from acp_gateway.agents.client import AgentClient
from acp_gateway.agents.errors import SessionNotFound
from acp_gateway.config import AgentProfile, AppConfig, SecretStore, Settings
from acp_gateway.connectors.launch import ConnectorLaunch, local_policies
from acp_gateway.connectors.maintenance import ConnectorMaintenance


def config(tmp_path):
    profile = AgentProfile(
        alias="claude", kind="claude_code", backend="claude_code", default_cwd=str(tmp_path)
    )
    return AppConfig(Settings(agents=[profile]), SecretStore(), None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
async def test_lost_claude_handle_is_terminal_without_transport_or_replay(tmp_path, routed):
    profile = config(tmp_path).settings.agents[0]
    if routed:
        profile = AgentProfile(
            alias="claude",
            kind="claude_code",
            backend="connector",
            computer_id="pc",
            default_cwd=str(tmp_path),
        )

    async def unexpected():
        pytest.fail("lost handle must not open a new generation")

    client = AgentClient(profile, pin_dir=tmp_path, transport_factory=unexpected)
    assert client.session_availability("old") == "restore-unavailable"
    with pytest.raises(SessionNotFound, match="cold resume"):
        await client.load_session("arbitrary-native-id")
    with pytest.raises(SessionNotFound, match="no automatic replay"):
        await client.ask("old", "never resend")


def test_claude_connector_manifest_and_maintenance_account_for_owned_work(tmp_path):
    cfg = config(tmp_path)
    assert list(local_policies(cfg)) == ["claude"]
    hello = ConnectorLaunch("ws://localhost/connect", "pc", tmp_path / "key").hello(cfg)
    assert hello.agents[0].kind == "claude_code"
    manager = SimpleNamespace(
        maintenance_reserved=False, maintenance_status=lambda: {"running_sessions": 1}
    )
    maintenance = ConnectorMaintenance(cfg.settings.agents, {"claude": manager})
    assert maintenance.status()["unsupported_agents"] == 0
    assert maintenance.status()["running_sessions"] == 1
    with pytest.raises(ValueError, match="busy"):
        maintenance.apply("prepare")
    manager.maintenance_status = lambda: {"running_sessions": 0}
    token = maintenance.apply("prepare")["token"]
    assert manager.maintenance_reserved
    maintenance.apply("resume", token)
    assert not manager.maintenance_reserved


@pytest.mark.asyncio
async def test_direct_claude_transport_selects_native_adapter(tmp_path, monkeypatch):
    import sys
    from unittest.mock import AsyncMock

    opened = AsyncMock(return_value=object())
    monkeypatch.setitem(
        sys.modules,
        "acp_gateway.adapters.claude_code",
        SimpleNamespace(ClaudeCodeAcpTransport=SimpleNamespace(open=opened)),
    )
    profile = config(tmp_path).settings.agents[0]
    client = AgentClient(profile, pin_dir=tmp_path)
    transport, pin = await client.open_transport()
    assert transport is opened.return_value and pin is None
    assert opened.await_args.args[0] == profile.claude_code
    assert opened.await_args.args[1].allowed_cwds == (str(tmp_path),)


def test_connector_uses_one_owned_claude_manager_and_closes_it(tmp_path, monkeypatch):
    import sys
    from unittest.mock import AsyncMock

    from acp_gateway.cli.main import main
    from acp_gateway.storage import Store

    path = tmp_path / "config.yaml"
    cfg = config(tmp_path)
    cfg.settings.data_dir = tmp_path / "data"
    path.write_text(cfg.settings.model_dump_json(exclude_none=True))
    key = tmp_path / "computer.key"
    store = Store.open_in(tmp_path / "data")
    store.computers.issue("pc", key, display_name="PC")
    store.close()
    instances = []

    class Manager:
        def __init__(self, settings, policy):
            assert settings == cfg.settings.agents[0].claude_code
            self.open = AsyncMock(
                return_value=SimpleNamespace(closed=__import__("asyncio").Event())
            )
            self.close = AsyncMock()
            self.maintenance_reserved = False
            instances.append(self)

        def maintenance_status(self):
            return {"running_sessions": 0}

    monkeypatch.setitem(
        sys.modules,
        "acp_gateway.adapters.claude_code",
        SimpleNamespace(ClaudeCodeProcessManager=Manager),
    )

    async def connect(url, **kwargs):
        assert kwargs["hello"].agents[0].kind == "claude_code"
        await kwargs["local_factory"]("claude")
        await kwargs["local_factory"]("claude")

    monkeypatch.setattr("acp_gateway.connectors.commands.run_connector", connect)
    assert (
        main(
            [
                "--config",
                str(path),
                "connector",
                "--dispatcher-url",
                "ws://127.0.0.1/connect",
                "--computer-id",
                "pc",
                "--token-file",
                str(key),
            ]
        )
        == 0
    )
    assert len(instances) == 1
    assert instances[0].open.await_count == 2
    instances[0].close.assert_awaited_once()
