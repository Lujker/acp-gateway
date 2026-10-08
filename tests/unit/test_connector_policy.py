"""Adversarial dispatcher frames must not widen the local agent's access."""

import asyncio
from copy import deepcopy

import pytest

from acp_gateway.connectors.policy import LocalAgentPolicy, LocalPolicyError, PolicyTransport

CWD = "/work/project"
MODE = "smart_approve"


def request(method, params, request_id=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if method != "session/cancel":
        message["id"] = request_id
    return message


@pytest.fixture
def policy():
    return LocalAgentPolicy((CWD,), MODE, max_prompt_length=10)


def test_initialize_removes_all_dispatcher_capabilities_and_extensions(policy):
    message = request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True},
                "terminal": True,
                "auth": {"terminal": True},
                "elicitation": {},
            },
            "clientInfo": {"name": "untrusted"},
            "_meta": {"capabilities": "anything"},
        },
    )
    before = deepcopy(message)
    assert policy.prepare(message)["params"] == {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
    }
    assert message == before


@pytest.mark.parametrize("method", ["session/new", "session/load"])
def test_session_creation_drops_executable_mcp_and_additional_roots(policy, method):
    params = {
        "cwd": CWD,
        "sessionId": "s1",
        "mcpServers": [{"name": "danger", "command": "sh", "args": ["-c", "dangerous"], "env": []}],
        "additionalDirectories": ["/etc"],
        "_meta": {"mcpServers": ["danger"]},
        "mcp_servers": ["alternate-field"],
        "url": "https://untrusted.invalid",
    }
    before = deepcopy(params)
    expected = {"cwd": CWD, "mcpServers": [], "additionalDirectories": []}
    if method == "session/load":
        expected["sessionId"] = "s1"
    assert policy.prepare(request(method, params))["params"] == expected
    assert params == before


@pytest.mark.parametrize(
    "cwd",
    [
        "/etc",
        "/work/project-evil",
        "/work/project/child",
        "/work/project/../project",
        "relative",
        "//work/project",
        "C:\\work",
        "\x00",
        [],
        "",
    ],
)
def test_cwd_never_escapes_exact_local_allowlist(policy, cwd):
    with pytest.raises(LocalPolicyError):
        policy.prepare(request("session/new", {"cwd": cwd}))


def test_list_defaults_to_local_cwd_and_safe_cursor(policy):
    assert policy.prepare(request("session/list", {"cursor": "page-2"}))["params"] == {
        "cwd": CWD,
        "cursor": "page-2",
    }


@pytest.mark.parametrize(
    "method",
    [
        "authenticate",
        "session/set_config_option",
        "session/fork",
        "session/resume",
        "fs/write_text_file",
        "terminal/create",
        "_custom",
    ],
)
def test_non_allowlisted_methods_are_rejected(policy, method):
    with pytest.raises(LocalPolicyError, match="method is not allowed"):
        policy.prepare(request(method, {}))


@pytest.mark.parametrize("mode", ["auto", "approve", "unknown", None])
def test_dispatcher_cannot_change_the_locally_chosen_mode(policy, mode):
    with pytest.raises(LocalPolicyError, match="mode is not allowed"):
        policy.prepare(request("session/set_mode", {"sessionId": "s1", "modeId": mode}))


@pytest.mark.parametrize(
    "blocks",
    [
        [],
        [{"type": "image", "data": "x"}],
        [{"type": "resource_link", "uri": "file:///etc/passwd"}],
        [{"type": "text", "text": "sixsix"}, {"type": "text", "text": "five5"}],
        [{"type": "text", "text": 3}],
    ],
)
def test_first_route_only_accepts_text_with_a_total_length_limit(policy, blocks):
    with pytest.raises(LocalPolicyError):
        policy.prepare(request("session/prompt", {"sessionId": "s1", "prompt": blocks}))


@pytest.mark.parametrize("value", [True, False, None, [], {}, -1, 2**53, "x" * 129])
def test_invalid_ids_are_rejected(policy, value):
    with pytest.raises(LocalPolicyError):
        policy.prepare(request("initialize", {"protocolVersion": 1}, value))


def test_policy_errors_do_not_include_rejected_values(policy):
    rejected = "private-" + "credential-123456"
    with pytest.raises(LocalPolicyError) as caught:
        policy.prepare(request("session/new", {"cwd": rejected}))
    assert rejected not in str(caught.value)


class RecordingTransport:
    def __init__(self):
        self.closed = asyncio.Event()
        self.sent = []
        self.frames = asyncio.Queue()

    async def send(self, message):
        self.sent.append(deepcopy(message))

    async def receive(self):
        return await self.frames.get()

    async def close(self):
        if not self.closed.is_set():
            self.closed.set()
            self.frames.put_nowait(None)


@pytest.fixture
def guarded(policy):
    raw = RecordingTransport()
    return PolicyTransport(raw, policy), raw


async def respond(guard, raw, result, request_id=1):
    raw.frames.put_nowait({"jsonrpc": "2.0", "id": request_id, "result": result})
    return await guard.receive()


async def attach(guard, raw, *, ready=True):
    await guard.send(request("session/new", {"cwd": CWD}))
    await respond(
        guard, raw, {"sessionId": "s1", "modes": {"currentModeId": MODE if ready else "auto"}}
    )


def prompt(request_id=3):
    return request(
        "session/prompt",
        {"sessionId": "s1", "prompt": [{"type": "text", "text": "hello"}]},
        request_id,
    )


async def permission(guard, raw, *, options=None):
    raw.frames.put_nowait(
        request(
            "session/request_permission",
            {
                "sessionId": "s1",
                "toolCall": {"toolCallId": "tool-1"},
                "options": options
                or [
                    {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
                    {"optionId": "no", "kind": "reject_once", "name": "Reject"},
                    {"optionId": "always", "kind": "allow_always", "name": "Always"},
                ],
            },
            77,
        )
    )
    return await guard.receive()


def decision(option):
    return {
        "jsonrpc": "2.0",
        "id": 77,
        "result": {"outcome": {"outcome": "selected", "optionId": option}},
    }


async def test_prompt_is_blocked_until_configured_mode_response(guarded):
    guard, raw = guarded
    await attach(guard, raw, ready=False)
    with pytest.raises(LocalPolicyError, match="mode has not been confirmed"):
        await guard.send(prompt())
    assert raw.closed.is_set()
    assert all(m.get("method") != "session/prompt" for m in raw.sent)


async def test_successful_mode_switch_enables_prompt(guarded):
    guard, raw = guarded
    await attach(guard, raw, ready=False)
    await guard.send(request("session/set_mode", {"sessionId": "s1", "modeId": MODE}, 2))
    await respond(guard, raw, {}, 2)
    await guard.send(prompt())
    assert raw.sent[-1]["method"] == "session/prompt"


async def test_failed_mode_switch_keeps_prompt_blocked(guarded):
    guard, raw = guarded
    await attach(guard, raw, ready=False)
    await guard.send(request("session/set_mode", {"sessionId": "s1", "modeId": MODE}, 2))
    raw.frames.put_nowait({"jsonrpc": "2.0", "id": 2, "error": {"code": -32603}})
    await guard.receive()
    with pytest.raises(LocalPolicyError):
        await guard.send(prompt())


async def test_mode_update_cannot_leave_session_ready_in_auto(guarded):
    guard, raw = guarded
    await attach(guard, raw)
    raw.frames.put_nowait(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s1",
                "update": {"sessionUpdate": "current_mode_update", "currentModeId": "auto"},
            },
        }
    )
    await guard.receive()
    with pytest.raises(LocalPolicyError):
        await guard.send(prompt())


@pytest.mark.parametrize("option", ["yes", "no"])
async def test_only_live_once_options_can_be_forwarded(guarded, option):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(prompt())
    forwarded = await permission(guard, raw)
    assert [o["optionId"] for o in forwarded["params"]["options"]] == ["yes", "no"]
    await guard.send(decision(option))
    assert raw.sent[-1] == decision(option)
    with pytest.raises(LocalPolicyError, match="no longer pending"):
        await guard.send(decision(option))


@pytest.mark.parametrize("option", ["always", "foreign", []])
async def test_disallowed_permission_reply_never_reaches_agent(guarded, option):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(prompt())
    await permission(guard, raw)
    count = len(raw.sent)
    with pytest.raises(LocalPolicyError):
        await guard.send(decision(option))
    assert len(raw.sent) == count
    assert raw.closed.is_set()


@pytest.mark.parametrize("finish", ["cancel", "result"])
async def test_late_approval_is_blocked_after_prompt_ends(guarded, finish):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(prompt())
    await permission(guard, raw)
    if finish == "cancel":
        await guard.send(request("session/cancel", {"sessionId": "s1"}))
    else:
        await respond(guard, raw, {"stopReason": "end_turn"}, 3)
    with pytest.raises(LocalPolicyError):
        await guard.send(decision("yes"))


async def test_cancelled_permission_outcome_still_works_after_cancel(guarded):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(prompt())
    await permission(guard, raw)
    await guard.send(request("session/cancel", {"sessionId": "s1"}))
    cancelled = {"jsonrpc": "2.0", "id": 77, "result": {"outcome": {"outcome": "cancelled"}}}
    await guard.send(cancelled)
    assert raw.sent[-1] == cancelled


async def test_duplicate_option_ids_cannot_disguise_allow_always(guarded):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(prompt())
    with pytest.raises(LocalPolicyError, match="duplicate permission options"):
        await permission(
            guard,
            raw,
            options=[
                {"optionId": "same", "kind": "allow_always", "name": "Always"},
                {"optionId": "same", "kind": "reject_once", "name": "Reject"},
            ],
        )
    assert raw.closed.is_set()


async def test_unsupported_client_request_is_refused_locally(guarded):
    guard, raw = guarded
    raw.frames.put_nowait(request("terminal/create", {"command": "dangerous"}, 77))
    raw.frames.put_nowait({"jsonrpc": "2.0", "method": "_harmless_notification"})
    forwarded = await guard.receive()
    assert forwarded["method"] == "_harmless_notification"
    assert raw.sent == [
        {
            "jsonrpc": "2.0",
            "id": 77,
            "error": {"code": -32601, "message": "Client method is not supported"},
        }
    ]


@pytest.mark.parametrize(
    "frame",
    [
        [1],
        {"jsonrpc": "2.0", "id": True},
        {"jsonrpc": "2.0", "method": "session/update", "params": []},
    ],
)
async def test_malformed_agent_frames_close_stream_without_raw_errors(guarded, frame):
    guard, raw = guarded
    raw.frames.put_nowait(frame)
    with pytest.raises(LocalPolicyError):
        await guard.receive()
    assert raw.closed.is_set()


async def test_request_tracking_is_bounded(guarded, monkeypatch):
    guard, raw = guarded
    monkeypatch.setattr("acp_gateway.connectors.policy.MAX_PENDING", 2)
    for request_id in (1, 2):
        await guard.send(request("session/list", {}, request_id))
    with pytest.raises(LocalPolicyError, match="too many"):
        await guard.send(request("session/list", {}, 3))
    assert len(raw.sent) == 2
    assert raw.closed.is_set()


async def test_reload_failure_invalidates_previous_attachment(guarded):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(request("session/load", {"cwd": CWD, "sessionId": "s1"}, 2))
    raw.frames.put_nowait({"jsonrpc": "2.0", "id": 2, "error": {"code": -32603}})
    await guard.receive()
    with pytest.raises(LocalPolicyError, match="mode has not been confirmed"):
        await guard.send(prompt())


async def test_prompt_cannot_race_a_pending_mode_switch(guarded):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(request("session/set_mode", {"sessionId": "s1", "modeId": MODE}, 2))
    with pytest.raises(LocalPolicyError, match="mode has not been confirmed"):
        await guard.send(prompt())


async def test_parallel_prompts_in_one_session_are_rejected(guarded):
    guard, raw = guarded
    await attach(guard, raw)
    await guard.send(prompt())
    with pytest.raises(LocalPolicyError, match="active prompt"):
        await guard.send(prompt(4))


async def test_permission_without_a_live_prompt_is_rejected(guarded):
    guard, raw = guarded
    await attach(guard, raw)
    with pytest.raises(LocalPolicyError, match="no active prompt"):
        await permission(guard, raw)


async def test_session_tracking_is_bounded(guarded, monkeypatch):
    guard, raw = guarded
    monkeypatch.setattr("acp_gateway.connectors.policy.MAX_SESSIONS", 1)
    await attach(guard, raw)
    await guard.send(request("session/new", {"cwd": CWD}, 2))
    with pytest.raises(LocalPolicyError, match="too many attached sessions"):
        await respond(guard, raw, {"sessionId": "s2"}, 2)


async def test_policy_violation_does_not_close_other_streams(policy):
    first, second = RecordingTransport(), RecordingTransport()
    guarded_first = PolicyTransport(first, policy)
    guarded_second = PolicyTransport(second, policy)
    with pytest.raises(LocalPolicyError):
        await guarded_first.send(request("authenticate", {}))
    await guarded_second.send(request("session/list", {}))
    assert first.closed.is_set()
    assert not second.closed.is_set()


@pytest.mark.parametrize(
    "message",
    [
        [1],
        {"jsonrpc": "2.0", "id": True},
        {"jsonrpc": "2.0", "method": "session/new", "params": []},
    ],
)
async def test_malformed_dispatcher_frames_close_stream(guarded, message):
    guard, raw = guarded
    with pytest.raises(LocalPolicyError):
        await guard.send(message)
    assert raw.sent == []
    assert raw.closed.is_set()
