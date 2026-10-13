"""Live connector session ownership through fresh ACP and real relay streams."""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from acp_gateway.adapters.hermes_sessions import HermesSessionManager
from acp_gateway.agents import (
    AgentClient,
    AgentError,
    AgentUnavailable,
    SessionBusy,
    SessionNotFound,
    TransportDisconnected,
)
from acp_gateway.api.app import create_app
from acp_gateway.channels.cli import CliChannel
from acp_gateway.config import AgentProfile
from acp_gateway.connectors.maintenance import ConnectorMaintenance
from acp_gateway.connectors.policy import LocalAgentPolicy, LocalPolicyError, PolicyTransport
from acp_gateway.core import GatewayCore
from acp_gateway.core.errors import GatewayError, MaintenanceBlocked, UnknownSession
from acp_gateway.storage import Conversation, Store
from fakes.fake_hermes import FakeHermes


@pytest.fixture
async def live(tmp_path):
    async with FakeHermes(tmp_path / "locks") as backend:
        profile = AgentProfile(
            alias="hermes",
            kind="hermes",
            backend="hermes",
            default_cwd=str(tmp_path),
            hermes={"lock_dir": backend.directory},
        )
        local = AgentClient(profile, pin_dir=tmp_path / "local-pins")
        policy = LocalAgentPolicy.from_profile(profile)

        async def engine():
            transport, _ = await local.open_transport()
            return transport

        manager = HermesSessionManager(engine, policy)

        def client():
            return AgentClient(
                profile.model_copy(update={"backend": "connector", "computer_id": "home"}),
                pin_dir=tmp_path / "remote-pins",
                reconnect_delays=(),
                transport_factory=lambda: open_stream(manager, policy),
            )

        try:
            yield backend, manager, client
        finally:
            await manager.close()
            await local.close()


async def open_stream(manager, policy):
    return PolicyTransport(await manager.open(), policy)


@pytest.fixture
async def model_core(live, tmp_path):
    backend, manager, make_client = live
    client = make_client()
    core = GatewayCore(Store.open(tmp_path / "models.db"), {"hermes": client})
    try:
        yield backend, manager, client, core, Conversation("cli", "default", "hermes")
    finally:
        await core.close()


async def test_core_model_operations_are_scoped_and_do_not_create_or_switch_dialogues(model_core):
    backend, _, _, core, conv = model_core
    with pytest.raises(UnknownSession):
        await core.model_options(conv)
    assert not backend.sessions
    record = await core.new_session(conv)
    revision = core.store.active_revision(conv)
    assert (await core.model_options(conv))["dialogue_id"] == record.id
    foreign = await core.new_session(Conversation("cli", "other-owner", "hermes"))
    with pytest.raises(UnknownSession):
        await core.set_model(conv, dialogue_id=foreign.id, provider="provider-b", model="model-b")
    with pytest.raises(GatewayError, match="changed"):
        await core.set_model(
            conv, expected_revision=revision + 1, provider="provider-b", model="model-b"
        )
    result = await core.set_model(
        conv, expected_revision=revision, provider="provider-b", model="model-b"
    )
    assert result["status"] == "accepted" and result["dialogue_id"] == record.id
    assert not result["selection_changed"]
    assert core.active_session(conv).id == record.id
    assert (await core.model_options(conv))["pending"] is None
    assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1


async def test_core_selection_guard_runs_after_connection_preflight(model_core, monkeypatch):
    backend, _, client, core, conv = model_core
    first = await core.new_session(conv)
    second = await core.new_session(conv)
    core.switch_dialogue(conv, first.id)
    started, release = asyncio.Event(), asyncio.Event()
    original = client.ensure_connected

    async def preflight():
        started.set()
        await release.wait()
        await original()

    monkeypatch.setattr(client, "ensure_connected", preflight)
    operation = asyncio.create_task(core.set_model(conv, provider="provider-b", model="model-b"))
    try:
        await started.wait()
        assert core.maintenance_status()["model_operations"] == 1
        with pytest.raises(MaintenanceBlocked):
            core.prepare_maintenance()
        core.switch_dialogue(conv, second.id)
        release.set()
        with pytest.raises(GatewayError, match="changed before"):
            await operation
        assert not any(c["method"] == "config.set" for c in backend.calls)
        assert client.session_availability(first.acp_session_id) == "attached"
        assert core.maintenance_status()["model_operations"] == 0
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


async def test_core_reports_original_target_if_selection_changes_after_dispatch(model_core):
    backend, _, _, core, conv = model_core
    first = await core.new_session(conv)
    second = await core.new_session(conv)
    core.switch_dialogue(conv, first.id)
    backend.pause_model_method = "config.set"
    operation = asyncio.create_task(core.set_model(conv, provider="provider-b", model="model-b"))
    try:
        await backend.model_started.wait()
        core.switch_dialogue(conv, second.id)
        backend.model_release.set()
        result = await operation
        assert result["dialogue_id"] == first.id and result["selection_changed"]
        assert core.active_session(conv).id == second.id
        assert backend.models[first.binding["runtime_id"]] == ("provider-b", "model-b")
        assert backend.models[second.binding["runtime_id"]] == ("provider-a", "model-a")
    finally:
        backend.model_release.set()
        await asyncio.gather(operation, return_exceptions=True)


async def test_core_model_controls_refuse_running_jobs_and_maintenance(model_core):
    backend, _, _, core, conv = model_core
    await core.new_session(conv)
    job = await core.submit_selected(conv, "hold")
    try:
        with pytest.raises(Exception, match="current job"):
            await core.set_model(conv, provider="provider-b", model="model-b")
    finally:
        backend.release.set()
        await core.wait(job.id)
    token = core.prepare_maintenance()
    with pytest.raises(MaintenanceBlocked):
        await core.model_options(conv)
    core.resume_maintenance(token)
    assert (await core.model_options(conv))["model"] == "model-a"


async def test_core_confirmation_cannot_survive_switch_away_and_back(model_core):
    backend, _, _, core, conv = model_core
    backend.model_confirm = True
    first = await core.new_session(conv)
    second = await core.new_session(conv)
    core.switch_dialogue(conv, first.id)
    confirmation = await core.set_model(conv, provider="provider-b", model="model-b")
    token = confirmation["confirmation_token"]
    core.switch_dialogue(conv, second.id)
    core.switch_dialogue(conv, first.id)
    with pytest.raises(GatewayError, match="earlier selection"):
        await core.set_model(conv, provider="provider-b", model="model-b", confirmation_token=token)
    assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1
    confirmation = await core.set_model(conv, provider="provider-b", model="model-b")
    assert (
        await core.set_model(
            conv,
            provider="provider-b",
            model="model-b",
            confirmation_token=confirmation["confirmation_token"],
        )
    )["status"] == "accepted"
    assert core.active_session(conv).id == first.id


async def test_owner_api_model_control_authentication_scope_and_revision(model_core):
    backend, _, _, core, conv = model_core
    cli = CliChannel()
    core.add_channel(cli)
    app = create_app(core, cli, SecretStr("model-owner"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer model-owner"},
        ) as api,
    ):
        record = await core.new_session(conv)
        path = f"/sessions/{record.id}"
        body = {"provider": "provider-b", "model": "model-b"}
        revision = (await api.get("/sessions")).json()["active_revision"]
        assert (await api.post(path + "/model", json=body)).status_code == 422
        body["expected_revision"] = revision
        assert (
            await api.get(path + "/models", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await api.post(path + "/model", json=body, headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (await api.get(path + "/models?thread=another-owner")).status_code == 404
        assert (
            await api.post(path + "/model", json=body | {"thread": "another-owner"})
        ).status_code == 404
        assert (
            await api.post(path + "/model", json=body | {"expected_revision": revision + 1})
        ).status_code == 409
        assert (
            await api.post(path + "/model", json=body | {"api_key": "must-not-echo"})
        ).status_code == 422
        assert (
            await api.post(path + "/model", json=body | {"provider": "x --global"})
        ).status_code == 422
        response = await api.post(path + "/model", json=body)
        assert response.status_code == 200 and response.json()["status"] == "accepted"
        response = await api.get(path + "/models")
        assert response.status_code == 200 and response.json()["model"] == "model-b"
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1


async def test_maintenance_refuses_pending_turn_and_preserves_same_chat_after_resume(live):
    backend, manager, make_client = live
    client = make_client()
    controller = ConnectorMaintenance(
        [client.profile.model_copy(update={"backend": "hermes"})], {"hermes": manager}
    )
    task = None
    try:
        session = await client.new_session()
        task = asyncio.create_task(client.ask(session, "hold"))
        async with asyncio.timeout(3):
            while not manager.maintenance_status()["pending_requests"]:
                await asyncio.sleep(0.01)
        with pytest.raises(ValueError, match="refused"):
            controller.apply("prepare")
        backend.release.set()
        await task
        token = controller.apply("prepare")["token"]
        with pytest.raises(AgentUnavailable, match="reserved"):
            await manager.open()
        with pytest.raises(Exception, match="reserved"):
            await client.ask(session, "must not run")
        controller.apply("resume", token)
        assert (await client.ask(session, "same dialogue")).text == "same dialogue"
        assert len(backend.sessions) == 1
    finally:
        backend.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


async def test_lost_attachment_cannot_clear_maintenance_uncertainty(live):
    _, manager, make_client = live
    client = make_client()
    try:
        await client.new_session()
        await manager._engine.close()
        await manager._pump
        assert not manager._sessions and not manager._requests
        assert manager.maintenance_status()["uncertain_sessions"] > 0
    finally:
        await client.close()


@pytest.mark.parametrize("deferred", [False, True])
async def test_model_wire_selection_preserves_chat_and_blocks_maintenance_until_observed(
    live, deferred
):
    backend, manager, make_client = live
    backend.defer_models = deferred
    client = make_client()
    controller = ConnectorMaintenance(
        [client.profile.model_copy(update={"backend": "hermes"})], {"hermes": manager}
    )
    try:
        session = await client.new_session()
        binding = client.session_binding(session)
        assert client.supports_model_control
        assert (await client.model_options(session))["model"] == "model-a"
        result = await client.set_model(session, provider="provider-b", model="model-b")
        assert result["status"] == ("deferred" if deferred else "accepted")
        with pytest.raises(ValueError, match="refused"):
            controller.apply("prepare")
        if deferred:
            assert (await client.model_options(session))["model"] == "model-a"
            assert (await client.ask(session, "next turn")).text == "next turn"
        observed = await client.model_options(session)
        assert (observed["provider"], observed["model"]) == ("provider-b", "model-b")
        assert observed["pending"] is None
        token = controller.apply("prepare")["token"]
        controller.apply("resume", token)
        assert client.session_binding(session) == binding
        assert len(backend.sessions) == 1
        assert all(c["method"] not in {"session.resume", "session.activate"} for c in backend.calls)
    finally:
        await client.close()


async def test_model_controls_refuse_maintenance_without_losing_chat(live):
    backend, manager, make_client = live
    client = make_client()
    controller = ConnectorMaintenance(
        [client.profile.model_copy(update={"backend": "hermes"})], {"hermes": manager}
    )
    try:
        session = await client.new_session()
        token = controller.apply("prepare")["token"]
        with pytest.raises(Exception, match="reserved"):
            await client.set_model(session, provider="provider-b", model="model-b")
        assert client.session_availability(session) == "attached"
        assert not any(c["method"] == "config.set" for c in backend.calls)
        controller.apply("resume", token)
        assert (await client.ask(session, "same chat")).text == "same chat"
    finally:
        await client.close()


async def test_model_confirmation_round_trip_and_stale_target_keep_chat_usable(live):
    backend, _, make_client = live
    backend.model_confirm = True
    client = make_client()
    try:
        session = await client.new_session()
        confirmation = await client.set_model(session, provider="provider-b", model="model-b")
        assert confirmation["status"] == "confirmation_required"
        token = confirmation["confirmation_token"]
        with pytest.raises(Exception, match="refused"):
            await client.set_model(
                session, provider="provider-b", model="other", confirmation_token=token
            )
        assert client.session_availability(session) == "attached"
        assert (
            await client.set_model(
                session, provider="provider-b", model="model-b", confirmation_token=token
            )
        )["status"] == "accepted"
        assert (await client.model_options(session))["model"] == "model-b"
        with pytest.raises(Exception, match="refused"):
            await client.set_model(
                session, provider="provider-b", model="model-b", confirmation_token=token
            )
        assert (await client.ask(session, "same chat")).text == "same chat"
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == 2
    finally:
        await client.close()


async def test_model_confirmation_cannot_transfer_to_reconnected_stream(live):
    backend, _, make_client = live
    backend.model_confirm = True
    first, second = make_client(), make_client()
    try:
        session = await first.new_session()
        binding = first.session_binding(session)
        token = (await first.set_model(session, provider="provider-b", model="model-b"))[
            "confirmation_token"
        ]
        await first.close()
        second.remember_session_binding(session, binding)
        with pytest.raises(Exception, match="refused"):
            await second.set_model(
                session, provider="provider-b", model="model-b", confirmation_token=token
            )
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1
        assert (await second.ask(session, "same dialogue")).text == "same dialogue"
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("mutation", [False, True])
async def test_model_operation_drop_quarantines_only_mutations_and_never_replays(live, mutation):
    backend, manager, make_client = live
    first, second = make_client(), make_client()
    backend.pause_model_method = "config.set" if mutation else "model.options"
    operation = None
    try:
        session = await first.new_session()
        binding = first.session_binding(session)
        operation = asyncio.create_task(
            first.set_model(session, provider="provider-b", model="model-b")
            if mutation
            else first.model_options(session)
        )
        await asyncio.wait_for(backend.model_started.wait(), 2)
        with pytest.raises(SessionBusy, match="pending"):
            await first.ask(session, "must not run")
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        backend.model_release.set()
        async with asyncio.timeout(3):
            while manager._requests:
                await asyncio.sleep(0.01)
        second.remember_session_binding(session, binding)
        if mutation:
            assert manager._sessions[session].blocked
            assert first.session_availability(session) == "uncertain"
            with pytest.raises(SessionBusy, match="unknown"):
                await first.load_session(session)
            with pytest.raises(SessionNotFound):
                await second.model_options(session)
        else:
            assert not manager._sessions[session].blocked
            backend.pause_model_method = None
            assert (await second.model_options(session))["model"] == "model-a"
            assert (await second.ask(session, "same chat")).text == "same chat"
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == int(mutation)
        assert len(backend.sessions) == 1
    finally:
        backend.model_release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await first.close()
        await second.close()


async def test_model_control_does_not_adopt_foreign_ids_or_missing_capabilities(live):
    backend, _, make_client = live
    client = make_client()
    try:
        session = await client.new_session()
        with pytest.raises(SessionNotFound, match="known"):
            await client.model_options("desktop-private")
        client.agent_capabilities.field_meta["acp-gateway"].pop("modelControl")
        with pytest.raises(Exception, match="unavailable"):
            await client.set_model(session, provider="provider-b", model="model-b")
        assert not any(c["method"] in {"config.set", "model.options"} for c in backend.calls)
        assert (await client.ask(session, "same chat")).text == "same chat"
    finally:
        await client.close()


async def test_unsupported_upstream_model_control_preserves_chat(live):
    backend, _, make_client = live
    client = make_client()
    backend.model_error = -32601
    try:
        session = await client.new_session()
        with pytest.raises(Exception, match="unavailable"):
            await client.set_model(session, provider="provider-b", model="model-b")
        assert client.session_availability(session) == "attached"
        assert (await client.ask(session, "same chat")).text == "same chat"
    finally:
        await client.close()


async def test_idle_dialogue_survives_new_stream_without_any_hermes_load_rpc(live):
    backend, _, make_client = live
    first, second = make_client(), make_client()
    try:
        session = await first.new_session()
        binding = first.session_binding(session)
        assert binding["restore"] == "connector-live"
        assert first.agent_capabilities.load_session and not first.supports_cancel
        assert (await first.ask(session, "before reconnect")).text == "before reconnect"
        await first.close()
        second.remember_session_binding(session, binding)
        assert (await second.ask(session, "after reconnect")).text == "after reconnect"
        assert len(backend.sessions) == 1
        assert second.session_binding(session) == binding
        assert [c["method"] for c in backend.calls].count("client.capabilities") == 1
        assert all(c["method"] not in {"session.activate", "session.resume"} for c in backend.calls)
    finally:
        await first.close()
        await second.close()


async def test_dispatcher_restart_keeps_active_dialogue_and_provenance(live, tmp_path):
    backend, _, make_client = live
    path = tmp_path / "gateway.db"
    conversation = Conversation("telegram", "123", "home/hermes")
    first = GatewayCore(Store.open(path), {"home/hermes": make_client()})
    await first.start()
    session = await first.new_session(conversation)
    before = await first.submit_selected(conversation, "before")
    await first.wait(before.id, 3)
    await first.close()
    second = GatewayCore(Store.open(path), {"home/hermes": make_client()})
    await second.start()
    try:
        assert second.active_session(conversation).id == session.id
        assert second.active_session(conversation).availability == "restore-required"
        job = await second.submit_selected(conversation, "after")
        assert (await second.wait(job.id, 3)).answer == "after"
        assert job.session_id == session.id
        assert len(second.sessions(conversation)) == len(backend.sessions) == 1
    finally:
        await second.close()


async def test_load_cannot_take_over_an_attached_or_foreign_dialogue(live):
    backend, _, make_client = live
    first, other = make_client(), make_client()
    try:
        session = await first.new_session()
        with pytest.raises(SessionNotFound):
            await other.load_session(session)
        for foreign in ("desktop-private", "live-1", "stored-live-1"):
            with pytest.raises(SessionNotFound):
                await other.load_session(foreign)
        assert (await first.ask(session, "still owned")).text == "still owned"
        assert len(backend.sessions) == 1
    finally:
        await first.close()
        await other.close()


async def test_lost_hermes_attachment_cannot_be_imported_by_id(live):
    backend, manager, make_client = live
    first, second = make_client(), make_client()
    try:
        session = await first.new_session()
        binding = first.session_binding(session)
        for socket in backend.sockets:
            await socket.close()
        await asyncio.wait_for(manager._engine.closed.wait(), 3)
        await first.close()
        second.remember_session_binding(session, binding)
        with pytest.raises(SessionNotFound):
            await second.ask(session, "must not cold resume")
        fresh = await second.new_session()
        assert fresh != session
        assert (await second.ask(fresh, "explicit new")).text == "explicit new"
        assert len(backend.sessions) == 2
        assert not any(c["method"] in {"session.activate", "session.resume"} for c in backend.calls)
    finally:
        await first.close()
        await second.close()


async def test_manager_stream_capacity_and_overflow_are_bounded(live):
    _, manager, _ = live
    streams = [await manager.open() for _ in range(32)]
    with pytest.raises(AgentUnavailable, match="limit"):
        await manager.open()
    stream = streams[0]
    for number in range(129):
        stream.reply(number, {})
    assert stream.closed.is_set()
    assert await stream.receive() is None
    replacement = await manager.open()
    assert not replacement.closed.is_set()
    assert not manager._engine.closed.is_set()
    replacement.reply(0, {"text": "x" * (256 * 1024)})
    assert replacement.closed.is_set()
    byte_limited = await manager.open()
    for number in range(6):
        byte_limited.reply(number, {"text": "x" * (200 * 1024)})
    assert byte_limited.closed.is_set()
    assert not manager._engine.closed.is_set()


async def test_new_manager_does_not_accept_another_managers_handles(live, tmp_path):
    backend, manager, make_client = live
    first = make_client()
    other = HermesSessionManager(manager.factory, manager.policy)
    second = AgentClient(
        first.profile,
        pin_dir=tmp_path / "other-pins",
        reconnect_delays=(),
        transport_factory=lambda: open_stream(other, manager.policy),
    )
    try:
        session = await first.new_session()
        binding = first.session_binding(session)
        await first.close()
        second.remember_session_binding(session, binding)
        with pytest.raises(SessionNotFound):
            await second.ask(session, "foreign manager")
        assert len(backend.sessions) == 1
        assert not any(c["method"] == "prompt.submit" for c in backend.calls)
    finally:
        await first.close()
        await second.close()
        await other.close()


async def test_persisted_provenance_must_match_before_any_prompt(live):
    backend, _, make_client = live
    first, second = make_client(), make_client()
    try:
        session = await first.new_session()
        binding = first.session_binding(session) | {"stored_id": "different-stored-id"}
        await first.close()
        second.remember_session_binding(session, binding)
        with pytest.raises(SessionNotFound, match="identity"):
            await second.ask(session, "wrong identity")
        assert not any(c["method"] == "prompt.submit" for c in backend.calls)
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("prompt", ["hold", "permit"])
async def test_drop_during_turn_does_not_restore_or_replay_even_after_completion(live, prompt):
    backend, manager, make_client = live
    first, second = make_client(), make_client()
    turn = None
    permission = asyncio.Event()

    async def pending(request):
        permission.set()
        await asyncio.Future()

    try:
        first.set_permission_handler(pending)
        session = await first.new_session()
        binding = first.session_binding(session)
        turn = asyncio.create_task(first.ask(session, prompt))
        await asyncio.wait_for(backend.started.wait(), 3)
        if prompt == "permit":
            await asyncio.wait_for(permission.wait(), 3)
            old_permission = next(iter(manager._permissions))
        # Simulate the remote stream disappearing; the manager/driver stay alive.
        for stream in list(manager._streams):
            await stream.close()
        with pytest.raises(TransportDisconnected):
            await asyncio.wait_for(turn, 3)
        await first.close()
        backend.release.set()
        await asyncio.sleep(0.05)
        second.remember_session_binding(session, binding)
        with pytest.raises(SessionNotFound):
            await second.ask(session, "must not replay")
        assert len([c for c in backend.calls if c["method"] == "prompt.submit"]) == 1
        assert backend.replies == []
        assert not any(
            c["method"] in {"session.interrupt", "session.resume"} for c in backend.calls
        )
        fresh = await second.new_session()
        assert (await second.ask(fresh, "fresh result")).text == "fresh result"
        # Old response IDs/updates must not contaminate the new turn.
        assert fresh != session
        if prompt == "permit":
            stale_stream = await manager.open()
            await stale_stream.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1},
                }
            )
            await stale_stream.receive()
            with pytest.raises(LocalPolicyError, match="not owned"):
                await stale_stream.send(
                    {
                        "jsonrpc": "2.0",
                        "id": old_permission,
                        "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
                    }
                )
            assert backend.replies == []
    finally:
        if turn is not None:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        await first.close()
        await second.close()


async def test_model_intent_is_durable_before_host_reply_and_cancel_is_unknown(model_core):
    backend, _, _, core, conv = model_core
    record = await core.new_session(conv)
    backend.pause_model_method = "config.set"
    task = asyncio.create_task(core.set_model(conv, provider="provider-b", model="model-b"))
    try:
        await asyncio.wait_for(backend.model_started.wait(), 5)
        evidence = Store.open(core.store._conn.execute("PRAGMA database_list").fetchone()[2])
        try:
            assert evidence.models.state(record.id)["state"] == "intent"
        finally:
            evidence.close()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert core.store.models.state(record.id)["state"] == "unknown"
        with pytest.raises(SessionBusy, match="unknown"):
            await core.submit_selected(conv, "must not run")
        with pytest.raises(SessionBusy, match="unknown"):
            await core.set_model(conv, provider="provider-b", model="model-b")
        with pytest.raises(MaintenanceBlocked):
            core.prepare_maintenance()
        assert core.active_session(conv).availability == "uncertain"
    finally:
        backend.model_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_pending_survives_dispatcher_restart_and_readback_reconciles(live, tmp_path):
    backend, _, make_client = live
    conv = Conversation("cli", "default", "hermes")
    path = tmp_path / "restart-models.db"
    core = GatewayCore(Store.open(path), {"hermes": make_client()})
    await core.start()
    record = await core.new_session(conv)
    await core.set_model(conv, provider="provider-b", model="model-b")
    assert core.store.models.state(record.id)["state"] == "pending"
    await core.close()
    core = GatewayCore(Store.open(path), {"hermes": make_client()})
    try:
        await core.start()
        assert core.active_session(conv).id == record.id
        with pytest.raises(SessionBusy, match="pending"):
            await core.set_model(conv, provider="provider-b", model="model-b")
        with pytest.raises(MaintenanceBlocked):
            core.prepare_maintenance()
        result = await core.model_options(conv)
        assert result["model_state"]["state"] == "observed"
        assert result["model_state"]["observation_source"] == "model.options"
        core.resume_maintenance(core.prepare_maintenance())
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1
    finally:
        await core.close()


async def test_unknown_survives_restart_and_matching_readback_does_not_clear_it(live, tmp_path):
    backend, _, make_client = live
    conv = Conversation("cli", "default", "hermes")
    path = tmp_path / "unknown-models.db"
    core = GatewayCore(Store.open(path), {"hermes": make_client()})
    await core.start()
    record = await core.new_session(conv)
    await core.set_model(conv, provider="provider-b", model="model-b")
    # Simulate a process exit between durable intent and recording acknowledgement.
    core.store.models.begin(record.id, "provider-b", "model-b", core.store.active_revision(conv))
    await core.close()
    core = GatewayCore(Store.open(path), {"hermes": make_client()})
    try:
        await core.start()
        assert core.model_state(conv, record.id)["model_state"]["state"] == "unknown"
        assert (await core.model_options(conv))["model_state"]["state"] == "unknown"
        with pytest.raises(SessionBusy, match="unknown"):
            await core.submit_selected(conv, "must not run")
        with pytest.raises(SessionBusy, match="unknown"):
            await core.set_model(conv, provider="provider-b", model="model-b")
        assert core.active_session(conv).id == record.id
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1
    finally:
        await core.close()


async def test_model_preflight_failure_does_not_create_intent(model_core, monkeypatch):
    _, _, client, core, conv = model_core
    record = await core.new_session(conv)

    async def unavailable():
        raise AgentUnavailable("synthetic preflight failure")

    monkeypatch.setattr(client, "ensure_connected", unavailable)
    with pytest.raises(AgentUnavailable):
        await core.set_model(conv, provider="provider-b", model="model-b")
    assert core.store.models.state(record.id) is None
    core.resume_maintenance(core.prepare_maintenance())


@pytest.mark.parametrize("error, expected", [(-32601, "refused"), (-32000, "unknown")])
async def test_model_explicit_unsupported_is_distinct_from_unknown(model_core, error, expected):
    backend, _, _, core, conv = model_core
    record = await core.new_session(conv)
    backend.model_error = error
    with pytest.raises(AgentError):
        await core.set_model(conv, provider="provider-b", model="model-b")
    assert core.store.models.state(record.id)["state"] == expected
    assert core.store.models.unresolved_count() == (expected == "unknown")
    if expected == "refused":
        assert core.active_session(conv).availability == "attached"
        core.resume_maintenance(core.prepare_maintenance())


async def test_offline_model_state_api_requires_owner_and_conversation_scope(model_core):
    backend, _, _, core, conv = model_core
    cli = CliChannel()
    core.add_channel(cli)
    app = create_app(core, cli, SecretStr("model-owner"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer model-owner"},
        ) as api,
    ):
        record = await core.new_session(conv)
        core.store.models.begin(record.id, "provider-b", "model-b", 1)
        core.store.models.settle(record.id, "unknown")
        path = f"/sessions/{record.id}/model-state?agent=hermes"
        before = len(backend.calls)
        response = await api.get(path)
        assert response.status_code == 200
        assert response.json()["model_state"]["state"] == "unknown"
        assert len(backend.calls) == before
        assert (await api.get(path + "&thread=other")).status_code == 404
        assert (await api.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_deferred_model_evidence_waits_for_own_turn_and_fresh_readback(model_core):
    backend, _, _, core, conv = model_core
    record = await core.new_session(conv)
    backend.defer_models = True
    result = await core.set_model(conv, provider="provider-b", model="model-b")
    assert result["status"] == "deferred"
    assert result["model_state"]["state"] == "pending"
    assert not result["model_state"]["turn_completed"]
    assert (await core.model_options(conv))["model_state"]["state"] == "pending"
    job = await core.submit_selected(conv, "hello")
    completed = await core.wait(job.id, 5)
    assert completed.status.value == "completed"
    assert core.store.models.state(record.id)["turn_completed"]
    assert core.store.models.state(record.id)["state"] == "pending"
    assert (await core.model_options(conv))["model_state"]["state"] == "observed"


async def test_confirmation_authority_is_not_persisted(model_core):
    backend, _, _, core, conv = model_core
    record = await core.new_session(conv)
    backend.model_confirm = True
    result = await core.set_model(conv, provider="provider-b", model="model-b")
    state = core.store.models.state(record.id)
    assert state["state"] == "confirmation_required"
    assert "confirmation_token" not in state
    assert result["confirmation_token"] not in str(state)
    assert core.store.models.unresolved_count() == 0


async def test_defaults_are_shared_across_channels_and_apply_only_to_new_owned_chats(model_core):
    backend, _, _, core, conv = model_core
    first = await core.new_session(conv)
    before = list(backend.calls)
    chosen = core.set_model_default(conv, provider="provider-b", model="model-b")
    assert backend.calls == before
    other_channel = Conversation("telegram", "12345", conv.agent)
    assert core.model_default(other_channel) == chosen
    new = await core.new_session(other_channel)
    assert backend.models[first.binding["runtime_id"]] == ("provider-a", "model-a")
    assert backend.models[new.binding["runtime_id"]] == ("provider-b", "model-b")
    assert core.store.models.state(new.id)["state"] == "pending"
    calls = [c for c in backend.calls if c["method"] == "config.set"]
    assert len(calls) == 1
    assert calls[0]["params"]["session_id"] == new.binding["runtime_id"]
    assert "--session" in calls[0]["params"]["value"]
    core.clear_model_default(conv)
    third = await core.new_session(conv)
    assert backend.models[third.binding["runtime_id"]] == ("provider-a", "model-a")


async def test_defaults_survive_dispatcher_restart_and_do_not_cross_routes(live, tmp_path):
    _, _, make_client = live
    path = tmp_path / "default-restart.db"
    conv = Conversation("cli", "default", "hermes")
    core = GatewayCore(Store.open(path), {"hermes": make_client()})
    await core.new_session(conv)
    core.set_model_default(conv, provider="provider-b", model="model-b")
    await core.close()
    other = make_client()
    other.profile = other.profile.model_copy(update={"computer_id": "other"})
    core = GatewayCore(Store.open(path), {"hermes": make_client(), "other": other})
    try:
        assert core.model_default(conv)["selection"] == {
            "provider": "provider-b",
            "model": "model-b",
        }
        with pytest.raises(GatewayError, match="identify"):
            core.model_default(Conversation("cli", "default", "other"))
        record = await core.new_session(conv)
        assert (await core.model_options(conv))["model_state"]["state"] == "observed"
        assert core.active_session(conv).id == record.id
    finally:
        await core.close()


async def test_default_confirmation_blocks_prompt_until_explicit_confirmation(model_core):
    backend, _, _, core, conv = model_core
    await core.new_session(conv)
    core.set_model_default(conv, provider="provider-b", model="model-b")
    backend.model_confirm = True
    with pytest.raises(GatewayError, match="needs model confirmation"):
        await core.new_session(conv)
    active = core.active_session(conv)
    with pytest.raises(SessionBusy, match="default"):
        await core.submit_selected(conv, "must not run")
    result = await core.set_model(conv, provider="provider-b", model="model-b")
    assert result["status"] == "confirmation_required"
    await core.set_model(
        conv,
        provider="provider-b",
        model="model-b",
        confirmation_token=result["confirmation_token"],
    )
    assert core.active_session(conv).id == active.id
    assert core.store.channel_state("model_default_required", str(active.id)) is None


async def test_defaults_api_rejects_actor_keys_and_never_calls_host(model_core):
    backend, _, _, core, conv = model_core
    await core.new_session(conv)
    cli = CliChannel()
    core.add_channel(cli)
    app = create_app(core, cli, SecretStr("model-owner"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer model-owner"},
        ) as api,
    ):
        before = list(backend.calls)
        body = {"agent": "hermes", "provider": "provider-b", "model": "model-b"}
        assert (await api.put("/model-default", json=body | {"owner": "other"})).status_code == 422
        assert (await api.put("/model-default", json=body | {"endpoint": "bad"})).status_code == 422
        assert (await api.put("/model-default", json=body)).status_code == 200
        assert (await api.get("/model-default?agent=hermes")).json()["selection"] == {
            "provider": "provider-b",
            "model": "model-b",
        }
        assert (await api.delete("/model-default?agent=hermes")).json()["selection"] is None
        assert backend.calls == before


async def test_telegram_model_controls_use_owned_target_and_do_not_prompt(model_core):
    from aiogram import Bot
    from aiogram.methods import SendMessage
    from test_telegram import OWNER, TOKEN, feed

    from acp_gateway.channels.telegram import TelegramChannel
    from acp_gateway.config import TelegramSettings
    from fakes.telegram import FakeTelegramSession

    backend, _, _, core, _ = model_core
    session = FakeTelegramSession()
    channel = TelegramChannel(
        TelegramSettings(enabled=True, allowed_user_ids=[OWNER]),
        SecretStr(TOKEN),
        bot=Bot(TOKEN, session=session),
    )
    channel.core = core
    conv = Conversation("telegram", str(OWNER), "hermes")
    record = await core.new_session(conv)
    revision = core.store.active_revision(conv)
    try:
        await feed(channel, 1, "/model")
        assert str(record.id) in next(c.text for c in session.calls if isinstance(c, SendMessage))
        await feed(channel, 2, f"/model set provider-b model-b {record.id} {revision}")
        assert backend.models[record.binding["runtime_id"]] == ("provider-b", "model-b")
        await feed(channel, 3, "/model default provider-b model-b")
        assert core.model_default(conv)["selection"]["model"] == "model-b"
        await feed(channel, 4, "/model state")
        assert not any(c["method"] == "prompt.submit" for c in backend.calls)
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1
    finally:
        await channel.bot.session.close()


async def test_model_route_guard_runs_after_transport_preflight(model_core, monkeypatch):
    backend, _, client, core, conv = model_core
    await core.new_session(conv)
    changed = False
    original = client.ensure_connected

    async def preflight():
        nonlocal changed
        await original()
        changed = True

    def guard():
        if changed:
            raise GatewayError("Agent route changed")

    monkeypatch.setattr(client, "ensure_connected", preflight)
    with pytest.raises(GatewayError, match="route changed"):
        await core.set_model(conv, provider="provider-b", model="model-b", admission=guard)
    assert not any(c["method"] == "config.set" for c in backend.calls)
    assert core.store.models.unresolved_count() == 0


async def test_session_journal_is_bounded_and_idle_detached_handle_can_be_retired(model_core):
    backend, manager, client, core, conv = model_core
    record = await core.new_session(conv)
    sid = record.acp_session_id
    job = await core.submit_selected(conv, "hello")
    assert (await core.wait(job.id, 5)).status.value == "completed"
    evidence = manager.journal(sid)
    assert evidence["last_result"]["outcome"] == "completed"
    assert any(event["kind"] == "answer_chunk" for event in evidence["events"])
    for number in range(200):
        manager._record(sid, "test_event", ordinal=number)
    evidence = manager.journal(sid)
    assert len(evidence["events"]) == 128
    assert evidence["first_sequence"] > 1
    with pytest.raises(ValueError, match="detached"):
        manager.retire(sid)
    await client.close()
    assert not manager.sessions()[0]["attached"]
    assert manager.retire(sid) == {"retired": sid, "upstream_deleted": False}
    assert not manager.sessions()
    assert record.binding["runtime_id"] in backend.sessions
    assert sid not in manager._engine.driver._handles


async def test_late_prompt_result_is_journalled_without_unblocking_or_replaying(model_core):
    backend, manager, client, core, conv = model_core
    record = await core.new_session(conv)
    job = await core.submit_selected(conv, "hold")
    await asyncio.wait_for(backend.started.wait(), 5)
    await client.close()
    backend.release.set()
    async with asyncio.timeout(5):
        while manager.journal(record.acp_session_id)["last_result"] is None:
            await asyncio.sleep(0.01)
    evidence = manager.journal(record.acp_session_id)
    assert evidence["blocked"]
    assert evidence["last_result"]["outcome"] == "completed"
    assert any(e["kind"] == "stream_lost" for e in evidence["events"])
    with pytest.raises(ValueError, match="detached"):
        manager.retire(record.acp_session_id)
    await core.wait(job.id, 5)
    assert len([c for c in backend.calls if c["method"] == "prompt.submit"]) == 1


async def test_retirement_refuses_unresolved_model_activity(model_core):
    _, manager, client, core, conv = model_core
    record = await core.new_session(conv)
    await core.set_model(conv, provider="provider-b", model="model-b")
    await client.close()
    with pytest.raises(ValueError, match="detached"):
        manager.retire(record.acp_session_id)
    assert manager.sessions()


async def test_private_session_ipc_journal_and_retirement(model_core, tmp_path):
    from filelock import FileLock

    from acp_gateway.cli.connector_maintenance import LocalClient
    from acp_gateway.connectors.lifecycle import control_record
    from acp_gateway.connectors.maintenance import STATUS, watch

    _, manager, client, core, conv = model_core
    record = await core.new_session(conv)
    for _ in range(128):
        manager._record(record.acp_session_id, "answer_chunk", text="😀" * 512)
    directory = tmp_path / "ipc"
    directory.mkdir()
    controller = ConnectorMaintenance(
        [type("Profile", (), {"backend": "hermes", "alias": "hermes"})()], {"hermes": manager}
    )
    with FileLock(directory / "connector.lock"), control_record(directory) as instance:
        task = asyncio.create_task(watch(directory, instance, controller, interval=0.01))
        ipc = LocalClient(directory, timeout=15)
        try:
            async with asyncio.timeout(5):
                while not (directory / STATUS).exists():
                    await asyncio.sleep(0.01)
            response = await asyncio.to_thread(
                ipc.request,
                "POST",
                "/maintenance/journal",
                json={"agent": "hermes", "handle": record.acp_session_id},
            )
            assert response.status_code == 200
            assert len(response.json()["events"]) == 128
            await client.close()
            response = await asyncio.to_thread(
                ipc.request,
                "POST",
                "/maintenance/retire",
                json={"agent": "hermes", "handle": record.acp_session_id},
            )
            assert response.status_code == 200
            assert not response.json()["upstream_deleted"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def lose_model_reply(backend, manager, core, conv):
    record = core.active_session(conv)
    backend.pause_model_method = "config.set"
    operation = asyncio.create_task(core.set_model(conv, provider="provider-b", model="model-b"))
    await asyncio.wait_for(backend.model_started.wait(), 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert core.store.models.state(record.id)["state"] == "unknown"
    return record


async def test_exact_model_reconciliation_recovers_late_ack_without_repeating_set(model_core):
    backend, manager, _, core, conv = model_core
    await core.new_session(conv)
    record = await lose_model_reply(backend, manager, core, conv)
    revision = core.store.active_revision(conv)
    pending = await core.reconcile_model(conv, dialogue_id=record.id, expected_revision=revision)
    assert pending["reconciliation"] == "pending"
    assert pending["model_state"]["state"] == "unknown"
    backend.model_release.set()
    async with asyncio.timeout(5):
        while manager.journal(record.acp_session_id)["model_result"] is None:
            await asyncio.sleep(0.01)
    reconciled = await core.reconcile_model(conv, dialogue_id=record.id, expected_revision=revision)
    assert reconciled["reconciliation"] == "observed"
    assert core.active_session(conv).id == record.id
    assert not manager.journal(record.acp_session_id)["blocked"]
    assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1
    core.resume_maintenance(core.prepare_maintenance())


async def test_model_reconciliation_survives_dispatcher_restart_and_rejects_wrong_operation(
    model_core, live, tmp_path
):
    backend, manager, _, core, conv = model_core
    _, _, make_client = live
    await core.new_session(conv)
    record = await lose_model_reply(backend, manager, core, conv)
    backend.model_release.set()
    async with asyncio.timeout(5):
        while manager.journal(record.acp_session_id)["model_result"] is None:
            await asyncio.sleep(0.01)
    operation_id = core.store.channel_state("model_operation_id", str(record.id))
    await core.close()
    replacement = GatewayCore(Store.open(tmp_path / "models.db"), {"hermes": make_client()})
    try:
        await replacement.start()
        revision = replacement.store.active_revision(conv)
        replacement.store.set_channel_state("model_operation_id", str(record.id), "f" * 32)
        result = await replacement.reconcile_model(
            conv, dialogue_id=record.id, expected_revision=revision
        )
        assert result["reconciliation"] == "unavailable"
        assert result["model_state"]["state"] == "unknown"
        assert manager.journal(record.acp_session_id)["blocked"]
        replacement.store.set_channel_state("model_operation_id", str(record.id), operation_id)
        result = await replacement.reconcile_model(
            conv, dialogue_id=record.id, expected_revision=revision
        )
        assert result["model_state"]["state"] == "observed"
        assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1
    finally:
        await replacement.close()


async def test_model_reconciliation_cannot_release_unknown_host_failure(model_core):
    backend, _, _, core, conv = model_core
    record = await core.new_session(conv)
    backend.model_error = -32000
    with pytest.raises(AgentError):
        await core.set_model(conv, provider="provider-b", model="model-b")
    result = await core.reconcile_model(
        conv, dialogue_id=record.id, expected_revision=core.store.active_revision(conv)
    )
    assert result["reconciliation"] == "unknown"
    assert result["model_state"]["state"] == "unknown"
    with pytest.raises(SessionBusy):
        await core.submit_selected(conv, "must not run")
    assert len([c for c in backend.calls if c["method"] == "config.set"]) == 1


async def test_model_recovery_does_not_transfer_confirmation_authority(model_core):
    backend, manager, _, core, conv = model_core
    await core.new_session(conv)
    backend.model_confirm = True
    record = await lose_model_reply(backend, manager, core, conv)
    backend.model_release.set()
    async with asyncio.timeout(5):
        while manager.journal(record.acp_session_id)["model_result"] is None:
            await asyncio.sleep(0.01)
    result = await core.reconcile_model(
        conv, dialogue_id=record.id, expected_revision=core.store.active_revision(conv)
    )
    assert result["model_state"]["state"] == "refused"
    assert "confirmation_token" not in str(manager.journal(record.acp_session_id))
    assert backend.models[record.binding["runtime_id"]] == ("provider-a", "model-a")


async def test_model_refusal_owner_api_is_conflict_and_reconcile_checks_revision(model_core):
    backend, _, _, core, conv = model_core
    cli = CliChannel()
    core.add_channel(cli)
    app = create_app(core, cli, SecretStr("model-owner"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer model-owner"},
        ) as api,
    ):
        record = await core.new_session(conv)
        revision = core.store.active_revision(conv)
        backend.model_error = -32601
        response = await api.post(
            f"/sessions/{record.id}/model",
            json={"agent": "hermes", "provider": "p", "model": "m", "expected_revision": revision},
        )
        assert response.status_code == 409
        response = await api.post(
            f"/sessions/{record.id}/model-reconcile",
            json={"agent": "hermes", "expected_revision": revision + 1},
        )
        assert response.status_code == 409


@pytest.mark.parametrize("deferred", [False, True])
async def test_canonical_model_is_taken_from_ack_not_guessed_from_catalogue(model_core, deferred):
    backend, _, _, core, conv = model_core
    record = await core.new_session(conv)
    backend.canonical_model = "canonical-model"
    backend.defer_models = deferred
    result = await core.set_model(conv, provider="provider-b", model="model-b")
    assert result["model_state"]["requested_model"] == "model-b"
    assert result["model_state"]["resolved_request"]["model"] == "canonical-model"
    if deferred:
        assert (await core.model_options(conv))["model_state"]["state"] == "pending"
        job = await core.submit_selected(conv, "hello")
        assert (await core.wait(job.id, 5)).status.value == "completed"
    result = await core.model_options(conv)
    assert result["model_state"]["state"] == "observed"
    assert result["model_state"]["observed_model"] == "canonical-model"
    assert core.active_session(conv).id == record.id
