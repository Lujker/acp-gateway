"""Read-only update inventory shared by human interfaces and connector relay.

This first stage discovers installations; it deliberately exposes no update
executor. Inventory negotiation never authorizes package or container changes.
"""

import asyncio
import json
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import httpx
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from acp_gateway.agents.errors import AgentError
from acp_gateway.connectors.protocol import InstallationReport
from acp_gateway.installation import PACKAGE, REPOSITORY, installation_info, latest_stable_release
from acp_gateway.update_delivery import STABLE, official_image


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


def artifact_availability(report, version, *, image_lookup=None, package_lookup=None):
    """A release announcement alone does not imply this backend has an artifact."""
    if not version or report is None:
        return None, "unchecked"
    if not STABLE.fullmatch(version) or len(version) > 64:
        return None, "unsupported"
    if report.manager == "docker":
        return (
            (version, "published")
            if (image_lookup or official_image)(version)
            else (None, "unpublished")
        )
    if (
        report.manager == "uv"
        and report.source == "registry"
        and report.platform in {"linux", "win32"}
    ):
        return (
            (version, "published")
            if (package_lookup or published_uv_version)(version)
            else (None, "unpublished")
        )
    return None, "unsupported"


def available_stable_release(
    report, *, release_lookup=None, image_lookup=None, package_lookup=None
):
    """Read-only CLI entrypoint; exactly the same artifact gate as Telegram."""
    version = (release_lookup or latest_stable_release)()
    return artifact_availability(
        report,
        str(version) if version is not None else None,
        image_lookup=image_lookup,
        package_lookup=package_lookup,
    )


def published_uv_version(version, *, client=None):
    """Verify a release-specific PyPI artifact before stopping a managed service."""
    if not isinstance(version, str) or len(version) > 64 or not STABLE.fullmatch(version):
        raise ValueError("An exact stable version is required")
    context = (
        nullcontext(client)
        if client is not None
        else httpx.Client(timeout=10, follow_redirects=False)
    )
    with (
        context as session,
        session.stream("GET", f"https://pypi.org/pypi/{PACKAGE}/{version}/json") as response,
    ):
        if response.status_code == 404:
            return False
        response.raise_for_status()
        content = bytearray()
        for part in response.iter_bytes():
            content.extend(part)
            if len(content) > 262144:
                raise ValueError("PyPI release metadata exceeded its limit")
    value = json.loads(content)
    if not isinstance(value, dict) or not isinstance(value.get("info"), dict):
        raise ValueError("Invalid PyPI release metadata")
    info = value["info"]
    urls = info.get("project_urls")
    if (
        info.get("version") != version
        or not isinstance(urls, dict)
        or not any(str(url).rstrip("/") == REPOSITORY for url in urls.values())
    ):
        raise ValueError("PyPI release does not identify this project and exact version")
    files = value.get("urls")
    if not isinstance(files, list) or len(files) > 100:
        raise ValueError("Invalid PyPI release artifacts")
    for file in files:
        if not isinstance(file, dict) or file.get("yanked", False):
            continue
        filename = file.get("filename")
        expected = f"acp_gateway-{version}"
        usable = (
            file.get("packagetype") == "bdist_wheel" and filename == expected + "-py3-none-any.whl"
        ) or (
            file.get("packagetype") == "sdist"
            and filename in {expected + ".tar.gz", expected + ".zip"}
        )
        requires = file.get("requires_python") or info.get("requires_python") or ""
        if not isinstance(requires, str):
            raise ValueError("Invalid Python requirement in release metadata")
        if usable and Version("3.12") in SpecifierSet(requires):
            return True
    return False


def available_exact_release(report, version, *, client=None, image_lookup=None):
    if not isinstance(version, str) or len(version) > 64 or not STABLE.fullmatch(version):
        raise ValueError("An exact stable version is required")
    if (
        report.manager == "uv"
        and report.source == "registry"
        and report.platform in {"linux", "win32"}
    ):
        return (
            (version, "published")
            if published_uv_version(version, client=client)
            else (None, "unpublished")
        )
    return artifact_availability(report, version, image_lookup=image_lookup)


class UpdateInventory:
    """Single-owner application service; only read-only reports leave the host."""

    def __init__(
        self,
        dispatcher=None,
        *,
        local_report=None,
        release_lookup=None,
        image_lookup=None,
        package_lookup=None,
    ):
        self.dispatcher = dispatcher
        self.local_report = local_report or (lambda: installation_report(role="dispatcher"))
        self.release_lookup = release_lookup or latest_stable_release
        self.image_lookup = image_lookup or official_image
        self.package_lookup = package_lookup or published_uv_version
        self._artifacts = {}
        self.latest = None
        self.checked = None
        self._check_lock = asyncio.Lock()
        self._remote_limit = asyncio.Semaphore(4)

    async def check_release(self):
        async with self._check_lock:
            if self.checked is None or time.monotonic() - self.checked >= 60:
                latest = await asyncio.to_thread(self.release_lookup)
                self.latest = str(latest) if latest is not None else None
                self._artifacts.clear()
                self.checked = time.monotonic()
            return self.latest

    async def available_version(self, report):
        if not self.latest or report is None:
            return None, "unchecked"
        key = (report.manager, report.source, report.platform, self.latest)
        async with self._check_lock:
            if key not in self._artifacts:
                try:
                    self._artifacts[key] = await asyncio.to_thread(
                        artifact_availability,
                        report,
                        self.latest,
                        image_lookup=self.image_lookup,
                        package_lookup=self.package_lookup,
                    )
                except RELEASE_ERRORS:
                    return None, "unavailable"
        return self._artifacts[key]

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
            version, artifact_state = await self.available_version(report)
            target["available_version"] = version
            target["artifact_state"] = artifact_state
            target["newer_stable"] = (
                Version(version) > Version(report.installed_version)
                if version and report
                else False
                if self.latest
                else None
            )
        return {"targets": targets, "latest": self.latest, "more": len(rows) > (page + 1) * 10}


RELEASE_ERRORS = (OSError, ValueError, httpx.HTTPError)
