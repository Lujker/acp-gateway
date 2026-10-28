import asyncio
from types import SimpleNamespace

import pytest

from acp_gateway.adapters import runtime_inventory
from acp_gateway.adapters.claude_code import ClaudeCodeAcpTransport, ClaudeCodeProcessManager
from acp_gateway.adapters.codex import CodexAcpTransport, CodexProcessManager
from acp_gateway.agents.errors import AgentUnavailable
from acp_gateway.config import ClaudeCodeSettings, CodexSettings
from acp_gateway.connectors.protocol import RuntimeResult, RuntimeSessionRef, RuntimeSummary


@pytest.mark.parametrize(
    "manager_class,kind",
    [(CodexProcessManager, "codex"), (ClaudeCodeProcessManager, "claude_code")],
)
def test_inventory_is_bounded_private_and_does_not_refresh_activity(
    monkeypatch, manager_class, kind
):
    clock = [100.0]
    monkeypatch.setattr(runtime_inventory, "monotonic", lambda: clock[0])
    manager = manager_class(SimpleNamespace(wsl=None), None)
    activity = runtime_inventory.RuntimeActivity()
    stream = SimpleNamespace(
        _activity=activity,
        _closing=None,
        _sessions={"private-session": "private-directory"},
        _children={"private-session": {"process": SimpleNamespace(returncode=None)}},
        process=SimpleNamespace(returncode=None),
        _turns={"private-session": object()},
        _requests={1: object()},
        _permissions={2: object()},
        uncertain=0,
        cleanup_unconfirmed=False,
    )

    # SimpleNamespace is unhashable; manager normally stores transport instances.
    class Stream:
        pass

    owned = Stream()
    owned.__dict__.update(vars(stream))
    manager._streams.add(owned)
    clock[0] = 140
    first = manager.runtime_inventory()
    clock[0] = 145
    second = manager.runtime_inventory()
    assert first["kind"] == kind
    assert first["transports"][0] == {
        "handle": activity.handle,
        "idle_seconds": 40,
        "loaded_dialogues": 1,
        "owned_launchers": 1,
        "active_turns": 1,
        "pending_requests": 1,
        "pending_approvals": 1,
        "closing": False,
        "uncertain": False,
    }
    assert second["transports"][0]["idle_seconds"] == 45
    assert "private" not in repr(second)
    activity.touch()
    assert manager.runtime_inventory()["transports"][0]["idle_seconds"] == 0
    assert len(manager.maintenance_status()) == 5


def test_inventory_limits_rows_and_reports_truncation():
    manager = CodexProcessManager(SimpleNamespace(wsl=object()), None)

    class Stream:
        def __init__(self):
            self._activity = runtime_inventory.RuntimeActivity()
            self._closing = None
            self._sessions = {}
            self._turns = {}
            self._requests = {}
            self._permissions = {}
            self.process = SimpleNamespace(returncode=0)
            self.uncertain = 0
            self.cleanup_unconfirmed = False

    manager._streams = {Stream() for _ in range(33)}
    snapshot = manager.runtime_inventory()
    assert snapshot["environment"] == "wsl"
    assert snapshot["truncated"] is True
    assert len(snapshot["transports"]) == 32
    assert all(row["owned_launchers"] == 0 for row in snapshot["transports"])


class IdleStream:
    def __init__(self):
        self._activity = runtime_inventory.RuntimeActivity()
        self._activity.last_activity -= 100
        self._closing = None
        self.closed = asyncio.Event()
        self._sessions = {"first": "cwd", "second": "cwd"}
        self.sessions = object()
        self._turns = {}
        self._requests = {}
        self._permissions = {}
        self._pending = {}
        self._out = asyncio.Queue()
        self._events = asyncio.Queue()
        self.uncertain = 0
        self.cleanup_unconfirmed = False
        self.stopped = asyncio.Event()

    def _schedule_close(self):
        self.closed.set()
        self._closing = asyncio.create_task(self.stopped.wait())


@pytest.mark.parametrize(
    "fence",
    [
        "turn",
        "request",
        "approval",
        "native_request",
        "output",
        "event",
        "uncertain",
        "unconfirmed",
        "no_resume",
        "maintenance",
        "opening",
    ],
)
async def test_idle_cleanup_respects_fences(fence):
    manager = CodexProcessManager(SimpleNamespace(idle_timeout=10), None)
    stream = IdleStream()
    manager._streams.add(stream)
    if fence in {"turn", "request", "approval", "native_request"}:
        getattr(
            stream,
            {
                "turn": "_turns",
                "request": "_requests",
                "approval": "_permissions",
                "native_request": "_pending",
            }[fence],
        )[1] = object()
    elif fence in {"output", "event"}:
        getattr(stream, "_out" if fence == "output" else "_events").put_nowait({})
    elif fence == "uncertain":
        stream.uncertain = 1
    elif fence == "unconfirmed":
        stream.cleanup_unconfirmed = True
    elif fence == "no_resume":
        stream.sessions = None
    elif fence == "maintenance":
        manager.maintenance_reserved = True
    else:
        manager._opening = 1
    await manager.reap_idle()
    assert not stream.closed.is_set()


async def test_idle_cleanup_claims_transport_and_reports_closing_before_wait():
    manager = CodexProcessManager(SimpleNamespace(idle_timeout=10), None)
    stream = IdleStream()
    manager._streams.add(stream)
    task = asyncio.create_task(manager.reap_idle())
    await asyncio.sleep(0)
    assert stream.closed.is_set()
    assert manager.maintenance_status()["opening"] == 1
    stream.stopped.set()
    await task
    assert not manager._streams
    assert manager.maintenance_status()["opening"] == 0


async def test_idle_cleanup_disabled():
    manager = CodexProcessManager(SimpleNamespace(idle_timeout=0), None)
    stream = IdleStream()
    manager._streams.add(stream)
    await manager.reap_idle()
    assert not stream.closed.is_set()


async def test_historical_uncertainty_does_not_pin_independent_idle_transport():
    manager = CodexProcessManager(SimpleNamespace(idle_timeout=10), None)
    manager._uncertain = 1
    stream = IdleStream()
    manager._streams.add(stream)
    task = asyncio.create_task(manager.reap_idle())
    await asyncio.sleep(0)
    assert stream.closed.is_set()
    stream.stopped.set()
    await task
    assert manager.maintenance_status()["uncertain_sessions"] == 1


def test_aggregate_counts_include_transports_beyond_visible_limit():
    manager = CodexProcessManager(SimpleNamespace(wsl=None), None)

    class Stream:
        def __init__(self):
            self._activity = runtime_inventory.RuntimeActivity()
            self._closing = None
            self._sessions = {"sid": "/private"}
            self._turns = {}
            self._requests = {}
            self._permissions = {}
            self.process = SimpleNamespace(returncode=None)
            self.uncertain = 0
            self.cleanup_unconfirmed = False

    manager._streams = {Stream() for _ in range(33)}
    result = manager.runtime_inventory()
    assert len(result["transports"]) == 32 and result["truncated"]
    assert result["totals"]["owned_launchers"] == 33
    assert result["totals"]["loaded_dialogues"] == 33


async def test_reconnect_waits_for_cleanup_to_release_binding():
    manager = CodexProcessManager(SimpleNamespace(idle_timeout=10), None)
    stream = IdleStream()
    manager._streams.add(stream)
    cleanup = asyncio.create_task(manager.reap_idle())
    await asyncio.sleep(0)
    # Admission is checked after cleanup: no new transport can attach too early.
    manager.maintenance_reserved = True
    opening = asyncio.create_task(manager.open())
    await asyncio.sleep(0)
    assert not opening.done()
    stream.stopped.set()
    await cleanup
    with pytest.raises(AgentUnavailable, match="maintenance"):
        await opening


async def test_cleanup_failure_remains_uncertain():
    manager = CodexProcessManager(SimpleNamespace(idle_timeout=10), None)
    stream = IdleStream()

    def failed_close():
        stream.closed.set()

        async def fail():
            raise RuntimeError("unconfirmed stop")

        stream._closing = asyncio.create_task(fail())

    stream._schedule_close = failed_close
    manager._streams.add(stream)
    await manager.reap_idle()
    assert manager.maintenance_status()["uncertain_sessions"] == 1


async def test_cancelled_cleanup_keeps_admission_closed_until_stop():
    manager = CodexProcessManager(SimpleNamespace(idle_timeout=10), None)
    stream = IdleStream()
    manager._streams.add(stream)
    task = asyncio.create_task(manager.reap_idle())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not manager._reap_done.is_set()
    stream.stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager._reap_done.is_set()


@pytest.mark.parametrize("settings", [CodexSettings, ClaudeCodeSettings])
def test_idle_timeout_validation(settings):
    assert settings().idle_timeout == 1800
    assert settings(idle_timeout=0).idle_timeout == 0
    for value in (-1, 86401, True, 1.5):
        with pytest.raises(ValueError):
            settings(idle_timeout=value)


@pytest.mark.parametrize("kind", ["codex", "claude_code"])
def test_profile_config_omits_only_default_idle_timeout(kind, tmp_path):
    from acp_gateway.config import AgentProfile, omit_default_idle_timeout

    for timeout in (1800, 0, 42):
        profile = AgentProfile.model_validate(
            {
                "alias": "managed",
                "kind": kind,
                "backend": kind,
                "default_cwd": str(tmp_path),
                kind: {"idle_timeout": timeout},
            }
        )
        data = omit_default_idle_timeout(profile.model_dump(mode="json", exclude_none=True))
        if timeout == 1800:
            assert "idle_timeout" not in data[kind]
        else:
            assert data[kind]["idle_timeout"] == timeout
        assert getattr(AgentProfile.model_validate(data), kind).idle_timeout == timeout


def test_runtime_result_rejects_unbounded_or_inconsistent_data():
    values = dict(
        alias="code",
        kind="codex",
        environment="native",
        launchers=1,
        dialogues=1,
        turns=0,
        pending=0,
        approvals=0,
        opening=0,
        uncertain=False,
        closing=False,
        truncated=False,
    )
    profile = RuntimeSummary(**values)
    with pytest.raises(ValueError):
        RuntimeResult(page=2, pages=1, profiles=[])
    with pytest.raises(ValueError):
        RuntimeResult(status="busy", page=1, pages=1, profiles=[profile])
    with pytest.raises(ValueError):
        RuntimeResult(page=1, pages=1, profiles=[profile, profile])
    with pytest.raises(ValueError):
        RuntimeSummary(**values, native_session_id="private")


@pytest.mark.parametrize(
    "transport,method",
    [(CodexAcpTransport, "_shutdown_process"), (ClaudeCodeAcpTransport, "_shutdown_children")],
)
async def test_unconfirmed_stop_never_releases_session_binding(transport, method):
    decisions = []
    stream = SimpleNamespace(
        cleanup_unconfirmed=False,
        uncertain=0,
        sessions=SimpleNamespace(close=lambda **kwargs: decisions.append(kwargs)),
    )

    async def unconfirmed():
        stream.cleanup_unconfirmed = True
        stream.uncertain = 1

    setattr(stream, method, unconfirmed)
    await transport._shutdown(stream)
    assert decisions == [{"confirmed": False, "interrupted": False}]
    assert stream.uncertain == 1


def test_runtime_matches_only_requested_dialogues_without_returning_ids():
    profile = SimpleNamespace(
        alias="code", backend="codex", codex=SimpleNamespace(wsl=None, idle_timeout=1800)
    )
    stream = SimpleNamespace(
        closed=asyncio.Event(), _sessions={"own-id": "private", "foreign-id": "private"}
    )
    manager = SimpleNamespace(
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
    refs = [
        RuntimeSessionRef(alias="code", session_id="own-id"),
        RuntimeSessionRef(alias="code", session_id="absent"),
    ]
    result = runtime_inventory.connector_inventory([profile], {"code": manager}, 1, refs)
    assert result.matched == [0]
    assert "own-id" not in result.model_dump_json()
    assert "foreign-id" not in result.model_dump_json()
    stream.closed.set()
    assert (
        runtime_inventory.connector_inventory([profile], {"code": manager}, 1, refs).matched == []
    )
