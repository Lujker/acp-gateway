"""Dedicated ownership policy and wire interleaving; no model or external agent."""

import asyncio
import json

import pytest

from acp_gateway.adapters import hermes
from acp_gateway.adapters.hermes import HermesDedicatedClient, _DedicatedRpc
from acp_gateway.discovery.hermes import HermesDiscoveryError
from acp_gateway.discovery.hermes_rpc import HermesRpcClient


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.count = 0
        self.submit_status = "streaming"
        self.drop = None
        self.create_result = None
        self.before_response = None
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return json.dumps(frame)

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        method = frame.get("method")
        if method is None or method == self.drop:
            return
        if method == "session.create":
            self.count += 1
            result = self.create_result or {
                "session_id": f"live-{self.count}",
                "stored_session_id": f"stored-{self.count}",
            }
        elif method == "prompt.submit":
            result = {"status": self.submit_status}
        elif method == "client.capabilities":
            result = {"server_requests": False}
        else:
            raise AssertionError(method)
        if self.before_response:
            await self.before_response(frame)
        await self.incoming.put({"jsonrpc": "2.0", "id": frame["id"], "result": result})

    async def close(self):
        self.closed = True
        await self.incoming.put(None)

    async def event(self, session, kind="message.complete", payload=None, **extra):
        await self.incoming.put(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"session_id": session, "type": kind, "payload": payload, **extra},
            }
        )

    async def approval(self, session, rid, **extra):
        await self.incoming.put(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "approval",
                "params": {"session_id": session, "choices": ["once", "deny"], **extra},
            }
        )


@pytest.fixture
async def dedicated():
    socket = Socket()
    rpc = _DedicatedRpc(socket, version="0.21.6", timeout=0.05)
    async with HermesDedicatedClient(rpc, profile="default") as client:
        yield client, socket, rpc
    assert socket.closed and client.closed.is_set()


async def flush_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_create_uses_native_cwd_and_handles_not_arbitrary_desktop_ids(dedicated):
    client, socket, _ = dedicated
    chat = await client.create(cwd=r"C:\Users\vladz\work")
    assert chat.runtime_id == "live-1" and chat.stored_id == "stored-1"
    assert chat.handle not in {chat.runtime_id, chat.stored_id}
    assert client.state(chat.handle) == "idle"
    assert socket.sent[0]["params"] == {
        "profile": "default",
        "source": "acp_gateway",
        "title": "Dispatcher",
        "cwd": r"C:\Users\vladz\work",
        "cwd_explicit": True,
        "close_on_disconnect": False,
    }
    for foreign in ("desktop-live-id", chat.runtime_id, chat.stored_id):
        with pytest.raises(HermesDiscoveryError, match="not owned"):
            await client.submit(foreign, "hello")
    assert len(socket.sent) == 1


async def test_round_trip_events_filter_foreign_sessions_and_release_busy(dedicated):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")
    await client.submit(chat.handle, "hello")
    with pytest.raises(HermesDiscoveryError, match="busy"):
        await client.submit(chat.handle, "another")
    await socket.event("desktop", "message.delta", {"text": "private"})
    await socket.event(chat.runtime_id, "message.delta", {"text": "hello"})
    await socket.event(chat.runtime_id, payload={"status": "complete", "text": "hello"})
    delta = await asyncio.wait_for(client.events.get(), 1)
    complete = await asyncio.wait_for(client.events.get(), 1)
    assert delta == {"handle": chat.handle, "type": "message.delta", "payload": {"text": "hello"}}
    assert complete["type"] == "message.complete"
    assert client.state(chat.handle) == "idle"
    await client.submit(chat.handle, "next")


async def test_completion_before_submit_ack_does_not_admit_second_inflight_prompt(dedicated):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")

    async def before(frame):
        if frame["method"] != "prompt.submit":
            return
        await socket.event(chat.runtime_id, payload={"status": "complete"})
        await flush_until(lambda: client.state(chat.handle) == "idle")
        with pytest.raises(HermesDiscoveryError, match="busy"):
            await client.submit(chat.handle, "second")

    socket.before_response = before
    await client.submit(chat.handle, "first")
    assert client.state(chat.handle) == "idle"
    assert len([f for f in socket.sent if f.get("method") == "prompt.submit"]) == 1


@pytest.mark.parametrize("status", ["queued", "steered", "redirected", "unknown"])
async def test_nonisolated_acceptance_fences_session_without_interrupt(dedicated, status):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")
    socket.submit_status = status
    with pytest.raises(HermesDiscoveryError, match="isolated"):
        await client.submit(chat.handle, "hello")
    await socket.event(chat.runtime_id, payload={"status": "complete"})
    await flush_until(lambda: client._rpc.events.empty())
    assert client.state(chat.handle) == "uncertain"
    with pytest.raises(HermesDiscoveryError, match="uncertain"):
        await client.submit(chat.handle, "retry")
    assert [f["method"] for f in socket.sent] == ["session.create", "prompt.submit"]


async def test_timeout_never_replays_or_restores_write_authority(dedicated):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")
    socket.drop = "prompt.submit"
    with pytest.raises(HermesDiscoveryError, match="outcome may be unknown"):
        await client.submit(chat.handle, "hello")
    with pytest.raises(HermesDiscoveryError, match="uncertain"):
        await client.submit(chat.handle, "retry")
    assert client.state(chat.handle) == "uncertain"
    assert len(socket.sent) == 2


async def test_requests_denied_only_for_own_running_chat(dedicated):
    client, socket, rpc = dedicated
    chat = await client.create(cwd="/work")
    await client.submit(chat.handle, "hello")
    await socket.approval("desktop", "foreign")
    await socket.approval(chat.runtime_id, "wrong-profile", profile="other")
    await socket.approval(chat.runtime_id, "own")
    await flush_until(lambda: any(f.get("id") == "own" for f in socket.sent))
    responses = {f["id"]: f for f in socket.sent if "method" not in f}
    assert responses["own"]["result"] == {"choice": "deny"}
    assert "foreign" not in responses and "wrong-profile" not in responses
    assert rpc.ignored_request_count >= 2


async def test_idle_own_request_is_ignored(dedicated):
    client, socket, rpc = dedicated
    chat = await client.create(cwd="/work")
    await socket.approval(chat.runtime_id, "idle")
    await flush_until(lambda: rpc.ignored_request_count == 1)
    assert all("method" in f for f in socket.sent)


@pytest.mark.parametrize(
    "method", ["session.resume", "session.activate", "session.interrupt", "config.set"]
)
async def test_no_resume_adoption_cancellation_or_administrative_rpc(dedicated, method):
    _, socket, rpc = dedicated
    with pytest.raises(HermesDiscoveryError, match="outside dedicated"):
        await rpc.call(method, {"session_id": "desktop", "profile": "default"})
    assert not socket.sent


async def test_identity_collision_closes_epoch_without_reusing_handles(dedicated):
    client, socket, _ = dedicated
    first = await client.create(cwd="/work")
    socket.create_result = {"session_id": first.runtime_id, "stored_session_id": "different"}
    with pytest.raises(HermesDiscoveryError, match="reused"):
        await client.create(cwd="/work")
    assert client.closed.is_set()
    with pytest.raises(HermesDiscoveryError, match="invalid"):
        await client.submit(first.handle, "hello")


async def test_connection_loss_invalidates_all_handles(dedicated):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")
    await socket.close()
    await asyncio.wait_for(client.closed.wait(), 1)
    with pytest.raises(HermesDiscoveryError, match="invalid"):
        await client.submit(chat.handle, "hello")
    assert len(socket.sent) == 1


@pytest.mark.parametrize("payload", [{"status": "interrupted"}, {}, {"status": "unknown"}])
async def test_unexpected_completion_loses_authority(dedicated, payload):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")
    await client.submit(chat.handle, "hello")
    await socket.event(chat.runtime_id, payload=payload)
    await flush_until(lambda: client.state(chat.handle) == "uncertain")
    assert (await client.events.get())["type"] == "gateway.uncertain"


async def test_foreign_turn_in_idle_chat_is_detected(dedicated):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")
    await socket.event(chat.runtime_id, "message.start")
    await flush_until(lambda: client.state(chat.handle) == "uncertain")
    with pytest.raises(HermesDiscoveryError, match="uncertain"):
        await client.submit(chat.handle, "hello")


async def test_limits_and_concurrent_creation(dedicated, monkeypatch):
    client, socket, _ = dedicated
    monkeypatch.setattr(hermes, "MAX_CONVERSATIONS", 2)
    first, second = await asyncio.gather(client.create(cwd="/a"), client.create(cwd="/b"))
    assert first.handle != second.handle and first.runtime_id != second.runtime_id
    with pytest.raises(HermesDiscoveryError, match="limit"):
        await client.create(cwd="/c")
    assert len(socket.sent) == 2


async def test_immediate_close_detaches_without_backend_mutations():
    socket = Socket()
    rpc = _DedicatedRpc(socket, version="0.21.6", timeout=0.05)
    client = HermesDedicatedClient(rpc, profile="default")
    await client.close()
    assert rpc.closed.is_set() and socket.closed and not socket.sent


async def test_event_overflow_fences_connection_without_interrupt(dedicated):
    client, socket, _ = dedicated
    chat = await client.create(cwd="/work")
    for _ in range(129):
        await socket.event(chat.runtime_id, "status.update", {"text": "tick"})
        await asyncio.sleep(0)
    await asyncio.wait_for(client.closed.wait(), 1)
    assert client.failure == "event_protocol_or_capacity_failure"
    assert [f["method"] for f in socket.sent] == ["session.create"]


async def test_unknown_create_outcome_closes_connection_without_retry(dedicated):
    client, socket, _ = dedicated
    socket.drop = "session.create"
    with pytest.raises(HermesDiscoveryError, match="outcome may be unknown"):
        await client.create(cwd="/work")
    with pytest.raises(HermesDiscoveryError, match="invalid"):
        await client.create(cwd="/work")
    assert len(socket.sent) == 1


async def test_opt_in_approvals_are_scoped_once_only_and_not_replayed(dedicated):
    client, socket, _ = dedicated
    client.approvals = True
    chat = await client.create(cwd="/work")
    other = await client.create(cwd="/other")
    await client.submit(chat.handle, "hello")
    await socket.approval(chat.runtime_id, "own", choices=["once", "session", "always", "deny"])
    event = await asyncio.wait_for(client.events.get(), 1)
    assert event["type"] == "approval.request"
    assert event["payload"]["choices"] == ("once", "deny")
    for handle, choice in ((other.handle, "once"), (chat.handle, "always")):
        with pytest.raises(HermesDiscoveryError, match="outside dedicated"):
            await client.respond_approval(handle, "own", choice)
    assert await client.respond_approval(chat.handle, "own", "once")
    assert not await client.respond_approval(chat.handle, "own", "once")
    assert len([f for f in socket.sent if "method" not in f]) == 1


async def test_opt_in_approval_withdrawal_rejects_late_response(dedicated):
    client, socket, _ = dedicated
    client.approvals = True
    chat = await client.create(cwd="/work")
    await client.submit(chat.handle, "hello")
    await socket.approval(chat.runtime_id, "own")
    await asyncio.wait_for(client.events.get(), 1)
    await socket.event(chat.runtime_id, "request.cancel", {"id": "own", "reason": "timeout"})
    assert (await asyncio.wait_for(client.events.get(), 1))["type"] == "request.cancel"
    assert not await client.respond_approval(chat.handle, "own", "once")
    assert not [f for f in socket.sent if "method" not in f]


class ModelSocket(Socket):
    def __init__(self):
        super().__init__()
        self.catalogue = {
            "provider": "provider-a",
            "model": "model-a",
            "api_key": "secret-must-stay-local",
            "providers": [{"slug": "provider-b", "models": ["model-b"], "api_key": "private"}],
        }
        self.ack = {"key": "model", "value": "model-b", "scope": "session"}
        self.model_started = asyncio.Event()
        self.model_release = None
        self.model_error = None

    async def send(self, data):
        frame = json.loads(data)
        if frame.get("method") not in {"config.set", "model.options"}:
            return await super().send(data)
        self.sent.append(frame)
        self.model_started.set()
        if self.model_release is not None:
            await self.model_release.wait()
        if frame["method"] == self.drop:
            return
        result = self.ack if frame["method"] == "config.set" else self.catalogue
        await self.incoming.put(
            {"jsonrpc": "2.0", "id": frame["id"]}
            | ({"error": {"code": self.model_error}} if self.model_error else {"result": result})
        )


@pytest.fixture
async def model_client():
    socket = ModelSocket()
    rpc = _DedicatedRpc(socket, version="0.21.6", timeout=0.05)
    async with HermesDedicatedClient(rpc, profile="default") as client:
        chat = await client.create(cwd="/work")
        yield client, chat, socket, rpc


async def test_model_selection_is_session_scoped_and_catalogue_is_projected(model_client):
    client, chat, socket, rpc = model_client
    options = await client.model_options(chat.handle)
    assert options["provider"] == "provider-a"
    assert options["providers"] == [{"provider": "provider-b", "models": ["model-b"]}]
    assert "secret" not in json.dumps(options) and "api_key" not in json.dumps(options)
    result = await client.set_model(chat.handle, provider="provider-b", model="model-b")
    assert result == {"status": "accepted"}  # No claim about a real provider call.
    assert socket.sent[-1]["params"] == {
        "profile": "default",
        "session_id": chat.runtime_id,
        "key": "model",
        "value": "model-b --provider provider-b --session",
        "confirm_expensive_model": False,
    }
    for method in ("config.set", "model.options"):
        with pytest.raises(HermesDiscoveryError, match="outside dedicated"):
            await rpc.call(method, socket.sent[-1]["params"])
    assert client.state(chat.handle) == "idle"
    assert client.model_activity(chat.handle)
    socket.catalogue |= {"provider": "provider-b", "model": "model-b"}
    assert (await client.model_options(chat.handle))["pending"] is None
    assert not client.model_activity(chat.handle)


@pytest.mark.parametrize("bad", ["--global", "x --global", "x\ny", "x\x00", "", "x" * 257])
async def test_model_identifiers_cannot_inject_flags_or_controls(model_client, bad):
    client, chat, socket, _ = model_client
    for provider, model in ((bad, "model-b"), ("provider-b", bad)):
        with pytest.raises(HermesDiscoveryError, match="identifier"):
            await client.set_model(chat.handle, provider=provider, model=model)
    assert len(socket.sent) == 1


async def test_model_operations_reject_foreign_handles_and_busy_turns(model_client):
    client, chat, socket, _ = model_client
    with pytest.raises(HermesDiscoveryError, match="not owned"):
        await client.model_options(chat.runtime_id)
    await client.submit(chat.handle, "hold")
    with pytest.raises(HermesDiscoveryError, match="busy"):
        await client.set_model(chat.handle, provider="provider-b", model="model-b")
    assert not any(f["method"] == "config.set" for f in socket.sent)


async def test_model_operation_blocks_prompt_and_other_model_operations(model_client):
    client, chat, socket, _ = model_client
    socket.model_release = asyncio.Event()
    operation = asyncio.create_task(
        client.set_model(chat.handle, provider="provider-b", model="model-b")
    )
    try:
        await socket.model_started.wait()
        assert client.model_activity(chat.handle)
        assert client.state(chat.handle) == "configuring"
        with pytest.raises(HermesDiscoveryError, match="busy"):
            await client.submit(chat.handle, "must not run")
        with pytest.raises(HermesDiscoveryError, match="busy"):
            await client.model_options(chat.handle)
        socket.model_release.set()
        assert await operation == {"status": "accepted"}
    finally:
        socket.model_release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("failure", ["timeout", "rpc_error", "global_scope", "invalid_ack"])
async def test_model_unknown_outcome_is_quarantined_without_retry(model_client, failure):
    client, chat, socket, _ = model_client
    if failure == "timeout":
        socket.drop = "config.set"
    elif failure == "rpc_error":
        socket.model_error = 5001
    elif failure == "global_scope":
        socket.ack["scope"] = "global"
    else:
        socket.ack["deferred"] = "yes"
    with pytest.raises(HermesDiscoveryError):
        await client.set_model(chat.handle, provider="provider-b", model="model-b")
    assert client.state(chat.handle) == "uncertain"
    with pytest.raises(HermesDiscoveryError, match="uncertain"):
        await client.submit(chat.handle, "must not replay")
    assert len([f for f in socket.sent if f["method"] == "config.set"]) == 1


async def test_confirmation_is_single_use_and_bound_to_chat_and_target(model_client):
    client, chat, socket, _ = model_client
    socket.ack |= {"confirm_required": True, "confirm_message": "Synthetic cost warning"}
    result = await client.set_model(chat.handle, provider="provider-b", model="model-b")
    assert result["status"] == "confirmation_required" and "warning" in result["message"]
    token = result["confirmation_token"]
    other = await client.create(cwd="/other")
    for handle, model in ((other.handle, "model-b"), (chat.handle, "other-model")):
        with pytest.raises(HermesDiscoveryError, match="stale"):
            await client.set_model(
                handle, provider="provider-b", model=model, confirmation_token=token
            )
    socket.ack["confirm_required"] = False
    result = await client.set_model(
        chat.handle, provider="provider-b", model="model-b", confirmation_token=token
    )
    assert result["status"] == "accepted"
    assert socket.sent[-1]["params"]["confirm_expensive_model"] is True
    socket.catalogue |= {"provider": "provider-b", "model": "model-b"}
    await client.model_options(chat.handle)
    with pytest.raises(HermesDiscoveryError, match="stale"):
        await client.set_model(
            chat.handle, provider="provider-b", model="model-b", confirmation_token=token
        )


async def test_prompt_invalidates_model_confirmation(model_client):
    client, chat, socket, _ = model_client
    socket.ack |= {"confirm_required": True, "confirm_message": "Synthetic warning"}
    token = (await client.set_model(chat.handle, provider="provider-b", model="model-b"))[
        "confirmation_token"
    ]
    await client.submit(chat.handle, "hello")
    await socket.event(chat.runtime_id, payload={"status": "complete"})
    await flush_until(lambda: client.state(chat.handle) == "idle")
    with pytest.raises(HermesDiscoveryError, match="stale"):
        await client.set_model(
            chat.handle, provider="provider-b", model="model-b", confirmation_token=token
        )


async def test_deferred_selection_requires_owned_turn_completion_and_fresh_observation(
    model_client,
):
    client, chat, socket, _ = model_client
    socket.ack["deferred"] = True
    assert await client.set_model(chat.handle, provider="provider-b", model="model-b") == {
        "status": "deferred"
    }
    with pytest.raises(HermesDiscoveryError, match="pending"):
        await client.set_model(chat.handle, provider="provider-b", model="model-b")
    socket.catalogue |= {"provider": "provider-b", "model": "model-b"}
    assert (await client.model_options(chat.handle))["pending"] is not None
    await socket.event("foreign", payload={"status": "complete"})
    assert (await client.model_options(chat.handle))["pending"] is not None
    await client.submit(chat.handle, "next turn")
    await socket.event(chat.runtime_id, payload={"status": "complete"})
    await flush_until(lambda: client.state(chat.handle) == "idle")
    assert client.model_activity(chat.handle)
    assert (await client.model_options(chat.handle))["pending"] is None
    assert not client.model_activity(chat.handle)


async def test_observer_does_not_gain_model_or_configuration_access():
    socket = Socket()
    rpc = HermesRpcClient(socket, version="0.21.6", timeout=0.05)
    try:
        for method in ("model.options", "config.set"):
            with pytest.raises(HermesDiscoveryError, match="allowlist"):
                await rpc.call(method, {"session_id": "desktop"})
        assert not socket.sent
    finally:
        await rpc.close()


async def test_model_method_not_found_keeps_chat_writable(model_client):
    client, chat, socket, _ = model_client
    socket.model_error = -32601
    with pytest.raises(HermesDiscoveryError, match="-32601"):
        await client.set_model(chat.handle, provider="provider-b", model="model-b")
    assert client.state(chat.handle) == "idle" and not client.model_activity(chat.handle)
    socket.model_error = None
    await client.submit(chat.handle, "same chat")


async def test_model_cancelled_delivery_is_not_replayed(model_client):
    client, chat, socket, _ = model_client
    socket.model_release = asyncio.Event()
    operation = asyncio.create_task(
        client.set_model(chat.handle, provider="provider-b", model="model-b")
    )
    await socket.model_started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert client.state(chat.handle) == "uncertain"
    assert not client._entry(chat.handle).models.busy
    assert len([f for f in socket.sent if f["method"] == "config.set"]) == 1


@pytest.mark.parametrize("stale", ["expired", "session_info", "catalogue_changed"])
async def test_model_confirmation_cannot_survive_stale_runtime(model_client, stale):
    client, chat, socket, _ = model_client
    await client.model_options(chat.handle)
    socket.ack |= {"confirm_required": True, "confirm_message": "Synthetic warning"}
    token = (await client.set_model(chat.handle, provider="provider-b", model="model-b"))[
        "confirmation_token"
    ]
    entry = client._entry(chat.handle)
    if stale == "expired":
        entry.models.confirmation = (*entry.models.confirmation[:3], 0)
    elif stale == "catalogue_changed":
        socket.catalogue["model"] = "changed-by-desktop"
        await client.model_options(chat.handle)
    else:
        await socket.event(chat.runtime_id, "session.info", {"model": "changed-by-desktop"})
        await flush_until(lambda: entry.models.confirmation is None)
    before = len(socket.sent)
    with pytest.raises(HermesDiscoveryError, match="stale"):
        await client.set_model(
            chat.handle, provider="provider-b", model="model-b", confirmation_token=token
        )
    assert len(socket.sent) == before
