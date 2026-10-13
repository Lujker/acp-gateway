"""Small Hermes host-v1 wire server: no provider, tools or upstream imports."""

import asyncio
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response


class FakeHermes:
    def __init__(self, directory):
        self.directory = directory
        self.token = secrets.token_urlsafe(32)
        self.calls = []
        self.replies = []
        self.sessions = {}
        self.sockets = []
        self.tasks = []
        self.questions = {}
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.withdrawn = asyncio.Event()
        self._question_sequence = 0
        self.models = {}
        self.pending_models = {}
        self.defer_models = False
        self.pause_model_method = None
        self.model_started = asyncio.Event()
        self.model_release = asyncio.Event()
        self.model_confirm = False
        self.model_error = None
        self.canonical_model = None

    async def __aenter__(self):
        self.server = await serve(self._handle, "127.0.0.1", 0, process_request=self._http)
        self.directory.mkdir(mode=0o700)
        record = {
            "role": "desktop-serve",
            "pid": 1234,
            "host": "127.0.0.1",
            "port": self.server.sockets[0].getsockname()[1],
            "profiles": ["default"],
            "protocolVersion": 1,
            "tokenFingerprint": hashlib.sha256(self.token.encode()).hexdigest()[:16],
        }
        for name, data in (("json", json.dumps(record)), ("token", self.token)):
            path = self.directory / f"host-desktop-serve.{name}"
            path.write_text(data)
            path.chmod(0o600)
        return self

    async def __aexit__(self, *args):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()

    def _http(self, connection, request):
        path = urlsplit(request.path).path
        if path == "/api/ws":
            assert parse_qs(urlsplit(request.path).query)["token"] == [self.token]
            return None
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return Response(401, "Unauthorized", Headers(), b"")
        value = (
            {"ok": True, "protocolVersion": 1, "pid": 1234, "role": "desktop-serve"}
            if path == "/api/host/identity"
            else {"version": "0.21.6"}
        )
        return Response(
            200, "OK", Headers({"Content-Type": "application/json"}), json.dumps(value).encode()
        )

    async def _send(self, socket, frame):
        await socket.send(json.dumps({"jsonrpc": "2.0", **frame}))

    async def event(self, socket, session, kind, payload=None):
        await self._send(
            socket,
            {
                "method": "event",
                "params": {"session_id": session, "type": kind, "payload": payload},
            },
        )

    async def _handle(self, socket):
        self.sockets.append(socket)
        async for raw in socket:
            frame = json.loads(raw)
            if "method" not in frame:
                self.replies.append(frame)
                future = self.questions.get(frame["id"])
                if future is not None and not future.done():
                    future.set_result(frame.get("result", {}).get("choice", "deny"))
                continue
            self.calls.append(frame)
            method, params = frame["method"], frame["params"]
            if method == "client.capabilities":
                result = params
            elif method == "ping":
                result = {"pong": True}
            elif method == "gateway.capabilities":
                result = {"per_session_exclusive_submit": True}
            elif method == "session.active_list":
                result = {"sessions": list(self.sessions)}
            elif method == "session.create":
                session = f"live-{len(self.sessions) + 1}"
                self.sessions[session] = params
                self.models[session] = ("provider-a", "model-a")
                result = {"session_id": session, "stored_session_id": f"stored-{session}"}
            elif method == "model.options":
                provider, model = self.models[params["session_id"]]
                result = {
                    "provider": provider,
                    "model": model,
                    "providers": [{"slug": "provider-b", "models": ["model-b"]}],
                }
            elif method == "config.set":
                if self.model_error is not None:
                    await self._send(
                        socket,
                        {
                            "id": frame["id"],
                            "error": {"code": self.model_error, "message": "synthetic model error"},
                        },
                    )
                    continue
                model, provider_flag, provider, scope = params["value"].split()
                assert params["key"] == "model" and provider_flag == "--provider"
                assert scope == "--session"
                model = self.canonical_model or model
                confirm_required = self.model_confirm and not params["confirm_expensive_model"]
                if confirm_required:
                    pass
                elif self.defer_models:
                    self.pending_models[params["session_id"]] = (provider, model)
                else:
                    self.models[params["session_id"]] = (provider, model)
                result = {
                    "key": "model",
                    "value": model,
                    "scope": "session",
                    "deferred": self.defer_models and not confirm_required,
                    "confirm_required": confirm_required,
                    "confirm_message": "Synthetic cost warning" if confirm_required else "",
                }
            elif method == "prompt.submit":
                if pending := self.pending_models.pop(params["session_id"], None):
                    self.models[params["session_id"]] = pending
                result = {"status": "queued" if params["text"] == "interfere" else "streaming"}
                await self._send(socket, {"id": frame["id"], "result": result})
                self.tasks.append(asyncio.create_task(self._turn(socket, params)))
                continue
            else:
                raise AssertionError(method)
            if method == self.pause_model_method:
                self.model_started.set()
                await self.model_release.wait()
            await self._send(socket, {"id": frame["id"], "result": result})

    async def _turn(self, socket, params):
        session, text = params["session_id"], params["text"]
        self.started.set()
        await self.event(socket, session, "message.start")
        if text == "hold":
            await self.release.wait()
            text = "released"
        elif text in {"permit", "withdraw"}:
            self._question_sequence += 1
            rid = f"srq-{self._question_sequence}"
            future = asyncio.get_running_loop().create_future()
            self.questions[rid] = future
            await self._send(
                socket,
                {
                    "id": "foreign",
                    "method": "approval",
                    "params": {"session_id": "desktop-private", "choices": ["once", "deny"]},
                },
            )
            await self._send(
                socket,
                {
                    "id": rid,
                    "method": "approval",
                    "params": {
                        "session_id": session,
                        "choices": ["once", "session", "always", "deny"],
                        "command": "synthetic command, never executed",
                        "description": "Test permission",
                    },
                },
            )
            if text == "withdraw":
                await asyncio.sleep(0.05)
                future.cancel()
                await self.event(
                    socket,
                    session,
                    "request.cancel",
                    {"id": rid, "reason": "timeout", "method": "approval"},
                )
                self.withdrawn.set()
                await self.release.wait()
                text = "withdrawn"
            else:
                text = "allowed" if await future == "once" else "denied"
        elif text == "chunks":
            await self.event(socket, "desktop-private", "message.delta", {"text": "private text"})
            await self.event(socket, session, "message.delta", {"text": "hello "})
            await self.event(socket, session, "message.delta", {"text": "world"})
            text = "hello world"
        await self.event(socket, session, "message.complete", {"status": "complete", "text": text})
