"""Hermes host rendezvous v1, shared by Desktop and independently run serve.

Only numeric loopback addresses are accepted. Records are hints until the
authenticated identity endpoint confirms the owner. No Hermes imports or
configuration changes are needed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROLES = ("desktop-serve", "serve")
MAX_RECORD_BYTES = 64 * 1024


class HermesDiscoveryError(ValueError):
    """Input-independent diagnostic, safe to display without credentials."""

    def __init__(self, message: str, *, status: str = "incompatible"):
        super().__init__(message)
        self.status = status


def default_lock_dir() -> Path:
    override = os.environ.get("HERMES_GATEWAY_LOCK_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise HermesDiscoveryError("Hermes lock directory must be absolute")
        return path
    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state.is_absolute():
        state = Path.home() / ".local" / "state"
    return state / "hermes" / "gateway-locks"


def _private_read(path: Path, limit: int) -> bytes:
    # On Windows read_endpoint performs ACL/reparse preflight before any bytes
    # are read; lstat/fstat also fence replacement while opening the leaf.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise HermesDiscoveryError("Hermes discovery file must be a regular file")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise HermesDiscoveryError("Hermes discovery files must be private to the current user")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise HermesDiscoveryError("Hermes discovery file changed while opening")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise HermesDiscoveryError("Hermes discovery file exceeds its size limit")
    return data


@dataclass(frozen=True)
class HermesEndpoint:
    role: str
    pid: int
    host: str
    port: int
    profiles: tuple[str, ...]
    token: str = field(repr=False)

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def check_identity(self, identity: Any) -> None:
        if not isinstance(identity, dict) or (
            identity.get("ok") is not True
            or type(identity.get("protocolVersion")) is not int
            or identity["protocolVersion"] != 1
            or type(identity.get("pid")) is not int
            or identity["pid"] != self.pid
            or identity.get("role") != self.role
        ):
            raise HermesDiscoveryError("Hermes backend identity does not match its record")


def read_endpoint(directory: Path, role: str) -> HermesEndpoint:
    if role not in ROLES:
        raise HermesDiscoveryError("unsupported Hermes backend role")
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
        raise HermesDiscoveryError("Hermes lock directory must be a real directory")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise HermesDiscoveryError("Hermes lock directory must be controlled by the current user")
    if sys.platform == "win32":
        from acp_gateway.windows_acl import check_private_paths

        paths = [directory / f"host-{role}.{suffix}" for suffix in ("json", "token")]
        # Preserve missing-file diagnostics; do not read either file before ACL checks.
        for path in paths:
            path.lstat()
        try:
            check_private_paths(paths, directories=[directory])
        except ValueError as exc:
            raise HermesDiscoveryError(str(exc), status="auth-required") from None
    try:
        record = json.loads(_private_read(directory / f"host-{role}.json", MAX_RECORD_BYTES))
        token = _private_read(directory / f"host-{role}.token", 1024).decode("utf-8").strip()
        if not isinstance(record, dict):
            raise ValueError
        address = ipaddress.ip_address(record["host"])
        pid, port = record["pid"], record["port"]
        profiles = record["profiles"]
        if (
            record.get("role") != role
            or type(record.get("protocolVersion")) is not int
            or record["protocolVersion"] != 1
            or not address.is_loopback
            or type(pid) is not int
            or pid <= 0
            or type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(profiles, list)
            or not 1 <= len(profiles) <= 100
            or any(
                not isinstance(p, str) or not p or len(p) > 256 or not p.isprintable()
                for p in profiles
            )
            or len(set(profiles)) != len(profiles)
            or not 32 <= len(token) <= 1024
            or not token.isascii()
            or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
        ):
            raise ValueError
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16]
        if record.get("tokenFingerprint") != fingerprint:
            raise HermesDiscoveryError(
                "Hermes token and record do not match; rediscover the backend"
            )
        return HermesEndpoint(role, pid, str(address), port, tuple(profiles), token)
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, HermesDiscoveryError):
            raise
        raise HermesDiscoveryError("invalid or unsupported Hermes discovery record") from None


async def _get_json(client: httpx.AsyncClient, path: str) -> dict:
    async with client.stream("GET", path) as response:
        if response.status_code in (401, 403):
            raise HermesDiscoveryError("Hermes authentication is required", status="auth-required")
        if response.status_code != 200:
            raise HermesDiscoveryError("Hermes backend refused the diagnostic request")
        data = bytearray()
        async for part in response.aiter_bytes():
            data.extend(part)
            if len(data) > MAX_RECORD_BYTES:
                raise HermesDiscoveryError("Hermes diagnostic response exceeds its size limit")
    try:
        result = json.loads(data)
    except (ValueError, RecursionError):
        raise HermesDiscoveryError("Hermes backend returned invalid diagnostic JSON") from None
    if not isinstance(result, dict):
        raise HermesDiscoveryError("Hermes backend returned an invalid diagnostic object")
    return result


async def verify_endpoint(endpoint: HermesEndpoint, *, timeout: float = 5) -> str:
    """Verify the current owner before WS; never follow redirects or use proxies."""
    async with httpx.AsyncClient(
        base_url=endpoint.base_url,
        headers={"Authorization": f"Bearer {endpoint.token}"},
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            endpoint.check_identity(await _get_json(client, "/api/host/identity"))
            status = await _get_json(client, "/api/status")
        except httpx.HTTPError:
            raise HermesDiscoveryError("Hermes backend is unavailable", status="offline") from None
    version = status.get("version")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 128
        or not version.isprintable()
    ):
        raise HermesDiscoveryError("Hermes backend did not report a valid version")
    return version
