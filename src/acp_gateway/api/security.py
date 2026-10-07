"""Reject untrusted hosts, credentials and oversized bodies before JSON parsing."""

import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from acp_gateway.config import is_loopback_host


class OwnerAccess:
    def __init__(self, app, *, token: str, max_body_bytes: int) -> None:
        self.app = app
        self._token = f"Bearer {token}".encode()
        self._limit = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            parts = urlsplit("http://" + headers.get(b"host", b"").decode("ascii"))
            valid_host = (
                parts.hostname is not None
                and is_loopback_host(parts.hostname)
                and parts.username is None
                and not parts.path
                and not parts.query
                and not parts.fragment
                and (parts.port is None or 1 <= parts.port <= 65535)
            )
        except (ValueError, UnicodeError):
            valid_host = False
        if not valid_host:
            await self._reject(scope, receive, send, 400, "a loopback Host header is required")
            return
        if not secrets.compare_digest(headers.get(b"authorization", b""), self._token):
            await self._reject(scope, receive, send, 401, "invalid bearer token")
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self._limit:
                await self._reject(scope, receive, send, 413, "request body is too large")
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def no_cache(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, replay, no_cache)

    @staticmethod
    async def _reject(scope, receive, send, status, detail):
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        await JSONResponse({"detail": detail}, status_code=status, headers=headers)(
            scope, receive, send
        )
