"""Real HTTP/WS against a minimal Hermes 0.21.6 wire contract; no model."""

import asyncio
import json
import secrets
from argparse import Namespace
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from acp_gateway.cli.main import main
from acp_gateway.discovery.hermes import HermesDiscoveryError, HermesEndpoint
from acp_gateway.discovery.hermes_rpc import HermesRpcClient, HermesRpcError

TOKEN = secrets.token_urlsafe(32)


async def test_dedicated_driver_verified_connection_and_scoped_create(backend):
    from acp_gateway.adapters.hermes import HermesDedicatedClient

    endpoint, calls, replies = backend
    async with await HermesDedicatedClient.connect(endpoint) as client:
        chat = await client.create(cwd=r"C:\work")
        assert chat.runtime_id == "live-1" and chat.stored_id == "stored-1"
        assert calls[0]["params"] == {"server_requests": False}
        with pytest.raises(HermesDiscoveryError, match="not owned"):
            await client.submit("desktop", "hello")
        with pytest.raises(HermesDiscoveryError, match="outcome may be unknown"):
            client._rpc.timeout = 0.05
            await client.submit(chat.handle, "fixed test")
        assert client.state(chat.handle) == "uncertain"
    assert [f["method"] for f in calls] == [
        "client.capabilities",
        "session.create",
        "prompt.submit",
    ]
    assert not replies


async def test_dedicated_profile_mismatch_fails_before_dial(backend):
    from acp_gateway.adapters.hermes import HermesDedicatedClient

    endpoint, calls, _ = backend
    with pytest.raises(HermesDiscoveryError, match="profile"):
        await HermesDedicatedClient.connect(endpoint, profile="other")
    assert not calls


@pytest.fixture
async def backend():
    calls, replies = [], []

    def http(connection, request):
        path = urlsplit(request.path).path
        if path == "/api/ws":
            assert parse_qs(urlsplit(request.path).query)["token"] == [TOKEN]
            return None
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        result = (
            {"ok": True, "protocolVersion": 1, "pid": 1234, "role": "desktop-serve"}
            if path == "/api/host/identity"
            else {"version": "0.21.6"}
        )
        data = json.dumps(result).encode()
        return Response(200, "OK", Headers({"Content-Type": "application/json"}), data)

    async def handler(socket):
        async for raw in socket:
            frame = json.loads(raw)
            if "method" not in frame:
                replies.append(frame)
                continue
            calls.append(frame)
            method = frame["method"]
            fault = frame.get("params", {}).get("test_fault")
            if fault == "disconnect":
                await socket.close()
                break
            if fault == "malformed":
                await socket.send("not json")
                continue
            if fault == "overflow":
                for _ in range(129):
                    await socket.send(json.dumps({"jsonrpc": "2.0", "method": "event"}))
                continue
            if method == "ping":
                result = {"pong": True}
            elif method == "gateway.capabilities":
                result = {"per_session_exclusive_submit": True}
            elif method == "session.active_list":
                result = {"sessions": []}
            elif method == "client.capabilities":
                result = {"server_requests": True}
            elif method == "session.create":
                result = {"session_id": "live-1", "stored_session_id": "stored-1"}
            elif method == "session.resume":
                assert frame["params"]["omit_messages"] is True
                result = {"session_id": "live-1", "session_key": "stored-1"}
            elif method == "session.list":
                request_session = frame["params"].get("request_session", "live-1")
                # Requests/notifications interleave with RPC responses.
                await socket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "srq-1",
                            "method": "approval",
                            "params": {"session_id": request_session, "choices": ["once", "deny"]},
                        }
                    )
                )
                await socket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "srq-2",
                            "method": "secret",
                            "params": {"session_id": request_session},
                        }
                    )
                )
                await socket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": {
                                "type": "message.delta",
                                "session_id": "live-1",
                                "payload": {"text": "hello"},
                            },
                        }
                    )
                )
                result = {"sessions": [{"id": "stored-1", "title": "test"}]}
            elif method == "session.activate":
                if frame["params"].get("session_id") == "live-1":
                    result = {"session_id": "live-1", "session_key": "stored-1"}
                    await socket.send(
                        json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result})
                    )
                    continue
                await socket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": frame["id"],
                            "error": {"code": 4001, "message": TOKEN},
                        }
                    )
                )
                continue
            elif method == "prompt.submit":
                # No reply: the uncertain mutation must not be replayed.
                continue
            else:
                raise AssertionError(method)
            await socket.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}))

    async with serve(handler, "127.0.0.1", 0, process_request=http) as server:
        endpoint = HermesEndpoint(
            "desktop-serve",
            1234,
            "127.0.0.1",
            server.sockets[0].getsockname()[1],
            ("default",),
            TOKEN,
        )
        yield endpoint, calls, replies


async def test_http_identity_rpc_interleaving_and_scoped_test_denial(backend):
    endpoint, calls, replies = backend
    async with await HermesRpcClient.connect(endpoint, diagnostic=True) as client:
        client.select_test_session("live-1", profile="default")
        result = await client.call("session.list")
        assert result["sessions"][0]["id"] == "stored-1"
        assert (await client.events.get())["params"]["type"] == "message.delta"
        for _ in range(100):
            if len(replies) == 2:
                break
            await asyncio.sleep(0.001)
        assert replies[0]["result"] == {"choice": "deny"}
        assert replies[1]["error"]["code"] == -32601
        assert client.version == "0.21.6"
        assert client.denied_requests == ["approval", "secret"]
        with pytest.raises(HermesRpcError, match="4001") as error:
            await client.call("session.activate", {"session_id": "missing", "omit_messages": True})
        assert TOKEN not in str(error.value)
        with pytest.raises(HermesDiscoveryError, match="allowlist"):
            await client.call("config.set", {"key": "anything"})
    assert client.closed.is_set()
    assert all(f["method"] != "config.set" for f in calls)


async def test_timeout_does_not_replay_or_leave_pending_request(backend):
    endpoint, calls, _ = backend
    async with await HermesRpcClient.connect(endpoint, timeout=0.1, diagnostic=True) as client:
        client.select_test_session("live-1", profile="default")
        with pytest.raises(HermesDiscoveryError, match="outcome may be unknown"):
            await client.call(
                "prompt.submit", {"profile": "default", "session_id": "live-1", "text": "hello"}
            )
        assert not client._pending
        assert (await client.call("ping"))["pong"]
    assert sum(f["method"] == "prompt.submit" for f in calls) == 1


async def test_identity_mismatch_stops_before_websocket(backend):
    endpoint, calls, _ = backend
    other = HermesEndpoint(
        endpoint.role, 999, endpoint.host, endpoint.port, endpoint.profiles, TOKEN
    )
    with pytest.raises(HermesDiscoveryError, match="identity"):
        await HermesRpcClient.connect(other)
    assert not calls


@pytest.mark.parametrize("resume", [False, True])
async def test_two_peer_spike_normalizes_stored_id_and_leaves_session_open(
    backend, monkeypatch, resume
):
    import spike_hermes

    endpoint, calls, _ = backend
    monkeypatch.setattr(spike_hermes, "read_endpoint", lambda directory, role: endpoint)
    report = await spike_hermes.scenario(
        Namespace(
            lock_dir=None,
            sessions=not resume,
            resume_session="stored-1" if resume else None,
            observe_session=None,
            allow_auto_continue=resume,
            role="desktop-serve",
            profile="default",
            timeout=1,
            prompt=False,
        )
    )
    assert report["same_live_session"] and report["same_stored_session"]
    assert report["remaining_peer_pong"]
    assert report["inference"] == "not_requested"
    assert [f["method"] for f in calls].count("session.create") == (0 if resume else 1)
    assert [f["method"] for f in calls].count("session.resume") == (1 if resume else 0)
    assert not any(f["method"] in {"prompt.submit", "session.close"} for f in calls)


async def test_observer_cannot_settle_desktop_requests_or_mutate(backend):
    endpoint, calls, replies = backend
    async with await HermesRpcClient.connect(endpoint) as client:
        await client.call("session.list")
        assert (await client.call("ping"))["pong"]
        assert not replies
        assert client.ignored_request_count == 2
        assert not client.denied_requests
        for method in ("session.create", "session.resume", "prompt.submit", "session.interrupt"):
            with pytest.raises(HermesDiscoveryError, match="observation"):
                await client.call(method, {"session_id": "live-1"})
        with pytest.raises(HermesDiscoveryError, match="approval handling"):
            await client.call("client.capabilities", {"server_requests": True})
        with pytest.raises(HermesDiscoveryError, match="history-free"):
            await client.call("session.activate", {"session_id": "live-1"})
        with pytest.raises(HermesDiscoveryError, match="diagnostic access"):
            client.select_test_session("live-1", profile="default")
    assert {f["method"] for f in calls} == {"session.list", "ping"}


async def test_diagnostic_grant_cannot_write_or_answer_another_session(backend):
    endpoint, calls, replies = backend
    async with await HermesRpcClient.connect(endpoint, diagnostic=True) as client:
        client.select_test_session("live-1", profile="default")
        await client.call("session.list", {"request_session": "desktop-owned"})
        assert (await client.call("ping"))["pong"]
        assert not replies
        assert client.ignored_request_count == 2
        for method in ("prompt.submit", "session.interrupt"):
            with pytest.raises(HermesDiscoveryError, match="outside the test session"):
                await client.call(method, {"session_id": "desktop-owned"})
        with pytest.raises(HermesDiscoveryError, match="already bound"):
            client.select_test_session("desktop-owned", profile="default")
        with pytest.raises(HermesDiscoveryError, match="outside the test session"):
            await client.call("session.interrupt", {"profile": "foreign", "session_id": "live-1"})
        with pytest.raises(HermesDiscoveryError, match=r"unsupported.*parameters"):
            await client.call(
                "prompt.submit",
                {
                    "profile": "default",
                    "session_id": "live-1",
                    "text": "x",
                    "confirm_truncate": True,
                },
            )
    assert {f["method"] for f in calls} == {"session.list", "ping"}


@pytest.mark.parametrize("fault", ["disconnect", "malformed", "overflow"])
async def test_protocol_failure_fences_pending_and_future_calls(backend, fault):
    endpoint, calls, _ = backend
    async with await HermesRpcClient.connect(endpoint) as client:
        with pytest.raises(HermesDiscoveryError, match="connection closed"):
            await client.call("ping", {"test_fault": fault})
        assert client.closed.is_set()
        assert not client._pending
        with pytest.raises(HermesDiscoveryError, match="unavailable"):
            await client.call("ping")
    assert len(calls) == 1


def test_cli_probe_works_without_gateway_config(tmp_path, capsys):
    assert (
        main(
            [
                "--config",
                str(tmp_path / "missing.yaml"),
                "agents",
                "probe",
                "hermes",
                "--lock-dir",
                str(tmp_path),
                "--json",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert all(row["status"] == "missing" for row in report["agents"])


@pytest.mark.parametrize("timeout", ["0", "nan", "inf", "61"])
def test_cli_probe_rejects_unbounded_timeout(timeout, capsys):
    assert main(["agents", "probe", "hermes", "--timeout", timeout]) == 1
    assert "timeout" in capsys.readouterr().out
