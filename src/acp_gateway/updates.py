"""Read-only update inventory shared by human interfaces and connector relay.

This first stage discovers installations; it deliberately exposes no update
executor. Inventory negotiation never authorizes package or container changes.
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx
from packaging.version import Version

from acp_gateway.agents.errors import AgentError
from acp_gateway.connectors.protocol import InstallationReport
from acp_gateway.installation import installation_info, latest_stable_release


def installation_report(*, role):
    info = installation_info()
    manager = info["manager"]
    if role == "dispatcher" and (
        Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    ):
        manager = "docker"
    return InstallationReport(
        installed_version=info["version"],
        manager=manager,
        source=info["source"],
        platform=sys.platform if sys.platform in {"linux", "win32", "darwin"} else "other",
    )


class UpdateInventory:
    """Single-owner application service; only read-only reports leave the host."""

    def __init__(self, dispatcher=None, *, local_report=None, release_lookup=None):
        self.dispatcher = dispatcher
        self.local_report = local_report or (lambda: installation_report(role="dispatcher"))
        self.release_lookup = release_lookup or latest_stable_release
        self.latest = None
        self.checked = None
        self._check_lock = asyncio.Lock()
        self._remote_limit = asyncio.Semaphore(4)

    async def check_release(self):
        async with self._check_lock:
            if self.checked is None or time.monotonic() - self.checked >= 60:
                latest = await asyncio.to_thread(self.release_lookup)
                self.latest = str(latest) if latest is not None else None
                self.checked = time.monotonic()
            return self.latest

    async def targets(self, *, page=0):
        if type(page) is not int or not 0 <= page <= 100:
            raise ValueError("invalid inventory page")
        local = await asyncio.to_thread(self.local_report)
        rows = self.dispatcher.status() if self.dispatcher is not None else []
        rows = sorted(rows, key=lambda row: row["computer_id"])
        selected = rows[page * 10 : (page + 1) * 10]

        async def connector(row):
            result = {"target": row["computer_id"], "role": "connector", "report": None}
            if not row["connected"]:
                return {**result, "state": "offline"}
            if row.get("update_inventory") is False:
                return {**result, "state": "unsupported"}
            async with self._remote_limit:
                try:
                    report = await self.dispatcher.update_info(row["computer_id"])
                except (AgentError, ConnectionError, TimeoutError, BufferError, ValueError):
                    return {**result, "state": "unavailable"}
            return {**result, "state": "ready" if report else "unavailable", "report": report}

        targets = [
            {"target": "dispatcher", "role": "dispatcher", "state": "ready", "report": local},
            *await asyncio.gather(*(connector(row) for row in selected)),
        ]
        for target in targets:
            report = target["report"]
            target["newer_stable"] = (
                Version(self.latest) > Version(report.installed_version)
                if self.latest and report
                else None
            )
        return {"targets": targets, "latest": self.latest, "more": len(rows) > (page + 1) * 10}


RELEASE_ERRORS = (OSError, ValueError, httpx.HTTPError)
