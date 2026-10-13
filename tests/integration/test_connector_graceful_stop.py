"""Real connector CLI process drains relay/Hermes attachment on a local stop request."""

import asyncio
import json
import sys

import pytest
import yaml
from filelock import FileLock

from acp_gateway.agents import AgentClient, TransportDisconnected
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.control import ControlDispatcher
from acp_gateway.connectors.lifecycle import RUNTIME_FILE, STOP_FILE
from acp_gateway.storage import Store
from fakes.fake_hermes import FakeHermes


@pytest.mark.parametrize("prompt", [None, "hold", "permit", "reconnect"])
async def test_real_connector_drains_without_interrupt_or_permission_reply(tmp_path, prompt):
    directory = tmp_path / "data"
    store = Store.open_in(tmp_path / "store")
    key = tmp_path / "computer.key"
    store.computers.issue("home", key, display_name="Home")
    dispatcher = ControlDispatcher(store.computers)
    async with FakeHermes(tmp_path / "locks") as backend:
        config = tmp_path / "computer.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(directory),
                    "agents": [
                        {
                            "alias": "hermes",
                            "kind": "hermes",
                            "backend": "hermes",
                            "default_cwd": str(tmp_path),
                            "hermes": {"lock_dir": str(backend.directory)},
                        }
                    ],
                }
            )
        )
        async with dispatcher.listen("127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "acp_gateway",
                "--config",
                str(config),
                "connector",
                "--dispatcher-url",
                f"ws://127.0.0.1:{port}/connect",
                "--computer-id",
                "home",
                "--token-file",
                str(key),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            remote = AgentClient(
                AgentProfile(
                    alias="hermes",
                    kind="hermes",
                    backend="connector",
                    computer_id="home",
                    default_cwd=str(tmp_path),
                ),
                pin_dir=tmp_path / "pins",
                reconnect_delays=(),
                transport_factory=lambda: dispatcher.open_agent("home", "hermes"),
            )
            turn = None
            try:
                async with asyncio.timeout(10):
                    while "home" not in dispatcher.active:
                        await asyncio.sleep(0.02)
                session = await remote.new_session()
                permission = asyncio.Event()

                async def pending_permission(request):
                    permission.set()
                    await asyncio.Future()

                remote.set_permission_handler(pending_permission)
                if prompt in {"hold", "permit"}:
                    turn = asyncio.create_task(remote.ask(session, prompt))
                    await asyncio.wait_for(backend.started.wait(), 5)
                    if prompt == "permit":
                        await asyncio.wait_for(permission.wait(), 5)
                if prompt == "reconnect":
                    assert (
                        await remote.ask(session, "before reconnect")
                    ).text == "before reconnect"
                    binding = remote.session_binding(session)
                    previous = dispatcher.active["home"]
                    old_transport = remote._transport
                    await previous.connection.close(code=1001)
                    await asyncio.wait_for(old_transport.closed.wait(), 5)
                    async with asyncio.timeout(10):
                        while (
                            "home" not in dispatcher.active
                            or dispatcher.active["home"].connection_id == previous.connection_id
                            or not dispatcher.active["home"].relay.ready.is_set()
                        ):
                            await asyncio.sleep(0.02)
                    assert (await remote.ask(session, "after reconnect")).text == "after reconnect"
                    assert remote.session_binding(session) == binding
                    assert len(backend.sessions) == 1
                    assert [c["method"] for c in backend.calls].count("client.capabilities") == 1
                record = json.loads((directory / RUNTIME_FILE).read_text())
                (directory / STOP_FILE).write_text(
                    json.dumps({"version": 1, "instance": record["instance"]})
                )
                _, stderr = await asyncio.wait_for(process.communicate(), 10)
                assert process.returncode == 0, stderr.decode(errors="replace")
                if turn is not None:
                    with pytest.raises(TransportDisconnected):
                        await asyncio.wait_for(turn, 5)
                assert not (directory / RUNTIME_FILE).exists()
                with FileLock(directory / "connector.lock", timeout=0):
                    pass
                async with asyncio.timeout(5):
                    while "home" in dispatcher.active:
                        await asyncio.sleep(0.02)
                assert backend.replies == []
                assert not any(
                    call["method"] in {"session.interrupt", "session.resume"}
                    for call in backend.calls
                )
                assert len(backend.sessions) == 1
                # External backend remains responsive after connector detaches.
                from acp_gateway.discovery.hermes import read_endpoint, verify_endpoint

                endpoint = read_endpoint(backend.directory, "desktop-serve")
                assert await verify_endpoint(endpoint) == "0.21.6"
            finally:
                await remote.close()
                if turn is not None:
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
    store.close()
