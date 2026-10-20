"""Durable applicant state, normal TLS validation and explicit plaintext consent."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import ssl
import stat
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from filelock import FileLock, Timeout
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from acp_gateway.agents.transport import _NoRedirectConnect
from acp_gateway.connectors.control import _WIRE_LOG, validate_url
from acp_gateway.log import register_secret


def private_write(path, content):
    from acp_gateway.cli.wizard import _atomic, _snapshot

    _snapshot(path)  # Never replace a symlink or non-regular file.
    _atomic(path, content)
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def private_read(path):
    if os.name == "nt":
        from acp_gateway.windows_acl import check_private_paths

        check_private_paths([path], directories=[path.parent], purpose="Pairing applicant state")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError("invalid pairing state file")
        if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise ValueError("pairing state must belong to this user and have private permissions")
        try:
            return json.loads(stream.read())
        except (ValueError, UnicodeError):
            raise ValueError("invalid pairing state file") from None


async def pairing_rpc(url, payload, *, allow_ws=False):
    parts = validate_url(url)
    if parts.scheme == "ws" and not allow_ws:
        raise ValueError("plaintext WS pairing requires explicit consent")
    endpoint = url.rstrip("/") + "/pair"
    try:
        async with _NoRedirectConnect(
            endpoint,
            ssl=ssl.create_default_context() if parts.scheme == "wss" else None,
            proxy=None,
            max_size=4096,
            max_queue=1,
            compression=None,
            open_timeout=5,
            close_timeout=1,
            logger=_WIRE_LOG,
        ) as ws:
            async with asyncio.timeout(5):
                await ws.send(json.dumps(payload))
                result = json.loads(await ws.recv())
        if not isinstance(result, dict) or "error" in result:
            raise ValueError(
                "pairing rejected or expired; inspect pending state before starting another request"
            )
        return result
    except InvalidStatus as exc:
        if exc.response.status_code == 404:
            raise LookupError("pairing endpoint not found") from None
        raise ValueError("dispatcher rejected pairing; verify endpoint and retry later") from None
    except (OSError, InvalidHandshake, ConnectionClosed, TimeoutError):
        raise OSError(
            "pairing unavailable; verify TLS trust/name and reachability; no downgrade attempted"
        ) from None


@contextmanager
def _state_lock(path):
    if os.name == "nt":
        from acp_gateway.windows_acl import ensure_private_directory

        ensure_private_directory(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with FileLock(path.with_suffix(".lock"), timeout=0, mode=0o600):
            yield
    except Timeout:
        raise ValueError(
            "another pairing wizard owns this applicant state; finish it first"
        ) from None


def _locked_async(method):
    @wraps(method)
    async def locked(self, *args, **kwargs):
        with _state_lock(self.path):
            return await method(self, *args, **kwargs)

    return locked


def _locked_sync(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with _state_lock(self.path):
            return method(self, *args, **kwargs)

    return locked


class PairingClient:
    def __init__(self, path: Path):
        self.path = path
        self.rpc = pairing_rpc

    def load(self):
        state = private_read(self.path)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("unsupported pairing state")
        register_secret(state["device_code"])
        return state

    def save(self, state):
        private_write(self.path, json.dumps(state, ensure_ascii=True).encode())

    @_locked_async
    async def begin(self, url, owner_id, name, *, target=None, allow_ws=False):
        if "://" not in url:
            url = "wss://" + url
        parts = validate_url(url)
        if parts.scheme == "ws" and not allow_ws:
            raise ValueError("plaintext WS pairing requires explicit consent")
        candidates = (
            [url]
            if parts.path and parts.path != "/"
            else [url.rstrip("/") + p for p in ("/connect", "/acpgw/connect", "/")]
        )
        if self.path.exists():
            state = self.load()
            if (
                state["input_url"] != url
                or state["owner_id"] != owner_id
                or state["name"] != name
                or state["target"] != target
            ):
                raise ValueError(
                    "a different pairing is pending; explicitly cancel it before changing "
                    "endpoint or target"
                )
        else:
            state = {
                "version": 1,
                "input_url": url,
                "url": candidates[0],
                "owner_id": owner_id,
                "name": name,
                "target": target,
                "allow_ws": allow_ws,
                "device_code": secrets.token_urlsafe(32),
            }
            self.save(state)  # Before the first side effect: lost request replies are recoverable.
        if "request" in state:
            return state["request"]
        for candidate in candidates:
            try:
                result = await self.rpc(
                    candidate,
                    {
                        "action": "request",
                        "owner_id": owner_id,
                        "device_code": state["device_code"],
                        "name": name,
                        "target": target,
                    },
                    allow_ws=allow_ws,
                )
            except LookupError:
                continue  # Only 404, only same origin, never TLS failure or redirect.
            state.update(url=candidate, request=result)
            self.save(state)
            return result
        raise ValueError("pairing endpoint not found; enter the dispatcher's full connector path")

    @_locked_async
    async def finish(self):
        state = self.load()
        if "connection" in state:
            return state["connection"]
        request = state.get("request")
        if request is None:
            raise ValueError("resume the request before checking approval")
        auth = {"request_id": request["request_id"], "device_code": state["device_code"]}
        result = await self.rpc(
            state["url"], {"action": "claim", **auth}, allow_ws=state["allow_ws"]
        )
        if result.get("status") not in {"issued", "active"}:
            return result
        token = result.get("credential", "")
        import re

        if not re.fullmatch(r"acpc_[A-Za-z0-9_-]{43}", token):
            raise ValueError("invalid credential response")
        register_secret(token)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", result.get("computer_id", "")):
            raise ValueError("invalid computer response")
        token_file = self.path.with_name(request["request_id"] + ".key")
        private_write(token_file, (token + "\n").encode())
        state["token_file"] = token_file.name
        state["issuance"] = {"computer_id": result["computer_id"], "token_file": token_file.name}
        self.save(state)
        acknowledgement = await self.rpc(
            state["url"], {"action": "ack", **auth, "credential": token}, allow_ws=state["allow_ws"]
        )
        if (
            acknowledgement.get("status") != "active"
            or acknowledgement.get("computer_id") != result["computer_id"]
        ):
            raise ValueError("activation was not acknowledged; rerun connect to recover")
        connection = {
            "dispatcher_url": state["url"],
            "computer_id": result["computer_id"],
            "token_file": str(token_file.absolute()),
            "tls_fingerprint": None,
        }
        state["connection"] = connection
        self.save(state)
        return connection

    @_locked_async
    async def cancel(self):
        state = self.load()
        if "request" in state:
            await self.rpc(
                state["url"],
                {
                    "action": "cancel",
                    "request_id": state["request"]["request_id"],
                    "device_code": state["device_code"],
                },
                allow_ws=state["allow_ws"],
            )
        self.path.unlink()

    @_locked_sync
    def reset(self, *, confirm=False):
        import time

        if not confirm:
            raise ValueError("changing pairing requires explicit confirmation")
        state = self.load()
        if (
            "connection" not in state
            and state.get("request", {}).get("expires", float("inf")) > time.time()
        ):
            raise ValueError("pending pairing must be cancelled, not silently replaced")
        request_id = state["request"]["request_id"]
        import re

        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("invalid pairing request ID")
        archive = self.path.with_name(request_id + ".completed.json")
        private_write(archive, json.dumps(state).encode())
        self.path.unlink()
