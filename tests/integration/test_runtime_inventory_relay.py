import asyncio
from types import SimpleNamespace

import pytest

from acp_gateway.adapters.runtime_inventory import connector_inventory
from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.control import ControlDispatcher, connect_control
from acp_gateway.connectors.protocol import (
    AgentManifest,
    ComputerDiagnostic,
    Hello,
    RuntimeResult,
    RuntimeSessionRef,
)
from acp_gateway.connectors.relay import RelayPeer
from acp_gateway.storage import Store


async def test_runtime_capability_over_websocket_and_legacy_fallback(tmp_path):
    store = Store.open_in(tmp_path / "data")
    token = tmp_path / "token"
    store.computers.issue("work", token, display_name="Work")
    dispatcher = ControlDispatcher(store.computers, heartbeat_seconds=5)
    profiles = [
        AgentProfile(alias=f"agent{i:02}", kind="codex", backend="codex", default_cwd=str(tmp_path))
        for i in range(9)
    ]
    stream = SimpleNamespace(
        closed=asyncio.Event(), _sessions={"own-session": "/private", "foreign-session": "/private"}
    )
    managers = {
        "agent08": SimpleNamespace(
            _streams=[stream],
            runtime_inventory=lambda: {
                "transports": [],
                "opening": 0,
                "uncertain": False,
                "truncated": False,
                "totals": {},
                "any_uncertain": False,
                "any_closing": False,
                "minimum_age": None,
            },
        )
    }
    hello = Hello(
        computer_id="work",
        agents=[AgentManifest(alias=p.alias, display_name="Codex") for p in profiles],
    )

    async def factory(alias):
        raise AssertionError("inventory must not start an agent")

    def runtime_handler(request):
        if request.action not in {None, "inventory"}:
            return RuntimeResult(
                page=1,
                pages=1,
                profiles=[],
                diagnostics=ComputerDiagnostic(
                    version="0.2.8",
                    updater="ready",
                    maintenance="idle",
                    running=0,
                    requests=0,
                    approvals=0,
                    opening=0,
                    probe="ok" if request.alias else "not_run",
                    probe_alias=request.alias,
                ),
            )
        return connector_inventory(profiles, managers, request.page, request.candidates)

    async with dispatcher.listen("127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/connect"
        for enabled, diagnostic_enabled in ((False, False), (True, False), (True, True)):
            connection, welcome = await connect_control(
                url,
                credential=token.read_text().strip(),
                hello=hello,
                runtime_inventory=enabled,
                runtime_diagnostics=diagnostic_enabled,
            )
            peer = RelayPeer(
                connection,
                welcome.connection_id,
                [p.alias for p in profiles],
                local_factory=factory,
                runtime_inventory=welcome.runtime_inventory == 1,
                runtime_diagnostics=welcome.runtime_diagnostics == 1,
                runtime_handler=runtime_handler,
            )
            task = asyncio.create_task(peer.run())
            await peer.ready.wait()
            try:
                if not enabled:
                    with pytest.raises(AgentUnavailable, match="unsupported"):
                        await dispatcher.runtime("work", 1)
                else:
                    result = await dispatcher.runtime(
                        "work", 2, [RuntimeSessionRef(alias="agent08", session_id="own-session")]
                    )
                    assert (result.page, result.pages) == (2, 2)
                    assert [p.alias for p in result.profiles] == ["agent08"]
                    assert result.profiles[0].launchers == 0
                    assert result.matched == [0]
                    assert "session" not in result.model_dump_json()
                    if diagnostic_enabled:
                        diagnostic = await dispatcher.runtime(
                            "work", 1, action="initialize", alias="agent08"
                        )
                        assert diagnostic.diagnostics.probe == "ok"
                    else:
                        with pytest.raises(AgentUnavailable, match="unsupported"):
                            await dispatcher.runtime("work", 1, action="diagnostics")
            finally:
                await connection.close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    store.close()
