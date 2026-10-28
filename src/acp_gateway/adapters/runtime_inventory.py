"""Bounded, local inventory of connector-owned managed transports."""

import asyncio
from time import monotonic
from uuid import uuid4

from acp_gateway.log import get_logger


class RuntimeActivity:
    def __init__(self):
        self.handle = uuid4().hex
        self.last_activity = monotonic()

    def touch(self):
        self.last_activity = monotonic()


async def wait_for_cleanup(task):
    """Keep the cleanup fence through repeated cancellation requests."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


def inventory(manager, kind, *, limit=32):
    """Return counts and opaque transport handles, never native identities or payloads.

    Reading this projection is not activity. Counts describe owned launchers,
    not an inventory of processes outside the connector.
    """
    manager._prune()
    streams = sorted(manager._streams, key=lambda stream: stream._activity.handle)
    now = monotonic()
    rows = []
    totals = dict.fromkeys(
        (
            "owned_launchers",
            "loaded_dialogues",
            "active_turns",
            "pending_requests",
            "pending_approvals",
        ),
        0,
    )
    any_uncertain = any_closing = False
    minimum_age = None
    for stream in streams:
        row = {
            "handle": stream._activity.handle,
            "idle_seconds": max(0, int(now - stream._activity.last_activity)),
            "loaded_dialogues": len(stream._sessions),
            "owned_launchers": (
                sum(child["process"].returncode is None for child in stream._children.values())
                if kind == "claude_code"
                else int(stream.process.returncode is None)
            ),
            "active_turns": len(stream._turns),
            "pending_requests": len(stream._requests),
            "pending_approvals": len(stream._permissions),
            "closing": stream._closing is not None,
            "uncertain": bool(stream.uncertain or stream.cleanup_unconfirmed),
        }
        for key in totals:
            totals[key] += row[key]
        any_uncertain |= row["uncertain"]
        any_closing |= row["closing"]
        minimum_age = (
            row["idle_seconds"] if minimum_age is None else min(minimum_age, row["idle_seconds"])
        )
        if len(rows) < limit:
            rows.append(row)
    return {
        "kind": kind,
        "environment": "wsl" if manager.settings.wsl else "native",
        "opening": manager._opening + getattr(manager, "_spawning", 0) + manager._reaping,
        "maintenance_reserved": manager.maintenance_reserved,
        "uncertain": bool(manager._uncertain),
        "transports": rows,
        "totals": totals,
        "any_uncertain": any_uncertain,
        "any_closing": any_closing,
        "minimum_age": minimum_age,
        "truncated": len(streams) > limit,
    }


async def reap_idle(manager):
    """Close entire idle transports, claiming them before yielding to other tasks."""
    timeout = manager.settings.idle_timeout
    if (
        not timeout
        or manager.maintenance_reserved
        or manager._opening
        or getattr(manager, "_spawning", 0)
        or manager._reaping
    ):
        return
    manager._prune()
    now = monotonic()
    candidates = []
    for stream in manager._streams:
        if (
            stream._closing is not None
            or stream.closed.is_set()
            or stream.uncertain
            or stream.cleanup_unconfirmed
            or (stream._sessions and stream.sessions is None)
            or stream._requests
            or stream._turns
            or stream._permissions
            or getattr(stream, "_pending", None)
            or not stream._out.empty()
            or (hasattr(stream, "_events") and not stream._events.empty())
            or now - stream._activity.last_activity < timeout
        ):
            continue
        # _schedule_close marks closed synchronously, before another send can
        # enter. Existing sends reserve _requests without yielding beforehand.
        stream._schedule_close()
        candidates.append(stream)
    if not candidates:
        return
    manager._reaping += len(candidates)
    manager._reap_done.clear()
    try:
        completion = asyncio.gather(*(s._closing for s in candidates), return_exceptions=True)
        results, cancelled = await wait_for_cleanup(completion)
        for stream, result in zip(candidates, results, strict=True):
            if isinstance(result, BaseException):
                stream.uncertain = max(stream.uncertain, 1)
        if cancelled:
            raise asyncio.CancelledError
    finally:
        manager._reaping -= len(candidates)
        manager._prune()
        manager._reap_done.set()


async def watch_idle(managers, *, interval=30):
    """Run while the connector owns its managers, including reconnects."""
    while True:
        await asyncio.sleep(interval)
        for manager in list(managers.values()):
            if hasattr(manager, "reap_idle"):
                try:
                    await manager.reap_idle()
                except Exception as exc:
                    get_logger(__name__).warning("idle cleanup failed", error=type(exc).__name__)


def connector_inventory(profiles, managers, page, candidates=()):
    """Expose aggregate counts only; never reveal native dialogue identities."""
    from acp_gateway.connectors.protocol import RuntimeResult, RuntimeSummary

    profiles = sorted(
        (p for p in profiles if p.backend in {"codex", "claude_code"}), key=lambda p: p.alias
    )
    pages = max(1, (len(profiles) + 7) // 8)
    if page > pages:
        page = pages
    rows = []
    for profile in profiles[(page - 1) * 8 : page * 8]:
        settings = getattr(profile, profile.backend)
        manager = managers.get(profile.alias)
        snapshot = manager.runtime_inventory() if manager else None
        totals = snapshot["totals"] if snapshot else {}
        rows.append(
            RuntimeSummary(
                alias=profile.alias,
                kind=profile.backend,
                environment="wsl" if settings.wsl else "native",
                launchers=totals.get("owned_launchers", 0),
                dialogues=totals.get("loaded_dialogues", 0),
                turns=totals.get("active_turns", 0),
                pending=totals.get("pending_requests", 0),
                approvals=totals.get("pending_approvals", 0),
                opening=snapshot["opening"] if snapshot else 0,
                uncertain=bool(snapshot and (snapshot["uncertain"] or snapshot["any_uncertain"])),
                closing=bool(snapshot and snapshot["any_closing"]),
                truncated=bool(snapshot and snapshot["truncated"]),
                idle_seconds=min(86400, snapshot["minimum_age"])
                if snapshot and snapshot["minimum_age"] is not None
                else None,
                idle_timeout=settings.idle_timeout,
            )
        )
    matched = []
    for index, candidate in enumerate(candidates):
        manager = managers.get(candidate.alias)
        if manager and any(
            not stream.closed.is_set() and candidate.session_id in stream._sessions
            for stream in manager._streams
        ):
            matched.append(index)
    return RuntimeResult(page=page, pages=pages, profiles=rows, matched=matched)
